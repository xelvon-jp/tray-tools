# script_runner_ai.py
# PHAL (script_runner.py) 独自の「AI の窓」設定と調査画面。
#
# 1-14 までは EDDy の投げ先設定 (editor 節) を借りていたが、今後は
# ダウンロードの自動化など PHAL 側の自動化が増えるので、投げ先は
# PHAL が自分の設定 (settings.json の script_runner 節) に持つ。
# EDDy の設定とは混ぜない。投げ先は相手ごとに窓の目印が違うので、
# 名前つきのプロファイル (ai_profiles) として複数持ち、選ばれて
# いる1件 (ai_profile_active) を使う。ai_target には有効な
# プロファイルの写しを保存のたびに同期しておく (旧キーだけを
# 読む側がそのまま動くように)。
#
# プロファイルの形は copilot_loop のプロファイル辞書にそろえる
# (editor/ai_target.build_custom_profile と同じ並び + 入力欄の
# クラス・名前)。入力欄の合わせ方は、まず AutomationId、無ければ
# クラスと名前 (copilot_loop._input_box が両方を見る)。
#
# 網頁の中の入力欄 (contenteditable) は AutomationId を持たない
# ことが多く、EDDy の検出ではページ全体の Document (RootWebArea)
# を掴んで失敗した (2026-11-14 実測)。なので調査は copilot_io の
# 深い走査 (scan) で候補を全部出し、人が選ぶ形にする。
#
# UIA は裏方プロセス (copilot_io_worker) の中だけ、という線引きは
# 従来どおり。窓の一覧だけは素の Win32 列挙 (editor の投げ先画面と
# 同じ作法で、UIA は使わない)。
import ctypes
import threading
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# PHAL のプロファイルの安定した名前 (設定の中で1つだけ持つ)。
PROFILE_NAME = "phal-ai"

# 欄のキーと表示名 (この並びで画面に出す)。
FIELD_LABELS = (
    ("label", "表示名"),
    ("process_name", "プロセス名"),
    ("window_class", "ウィンドウクラス"),
    ("window_title_contains", "タイトルに含む文字列"),
    ("input_automation_id", "入力欄の AutomationId"),
    ("input_class", "入力欄のクラス"),
    ("input_name", "入力欄の名前"),
    ("send_automation_id", "送信ボタンの AutomationId"),
    ("send_name", "送信ボタンの名前"),
    ("send_class", "送信ボタンのクラス"),
    ("app_id_prefix", "アプリIDの頭（任意）"),
)
FIELD_KEYS = tuple(key for key, _label in FIELD_LABELS)

# 自動化の切替 (settings.json の script_runner 節の直下に置く
# auto_* キー)。既定は全部 False (手動) で、実機で安全が確かめ
# られた段から1つずつ入れる (持ち主の方針、2026-11-14)。実行の
# 自動化 (auto_run、2026-11-15 追加) は、パネルを起動したあとに
# 新しく現れたファイルだけが対象で、起動時に既にあった分は
# 決して自動では走らせない (窓側が到着の前後を見分けている)。
AUTO_LABELS = (
    ("auto_dl", "DLを自動で押す"),
    ("auto_run", "実行を自動で始める"),
    # 受領応答 (1-51): 自動実行が始まる直前に「受け取って実行を
    # 始める」ことだけを先に送る。既定は入 (sanitize_automation
    # 参照。他の切替と違い、欠けていれば入に倒す)。
    ("auto_ack", "受領を自動で送る"),
    ("auto_answer", "回答を自動で入れる"),
    ("auto_send", "送信を自動で押す"),
    # 診断の自動応答 (1-43): AI の発言の中の目印の行を見つけたら、
    # 診断の束 (1-41) を入力欄へ入れて送信まで自動でやる。呼ぶのは
    # AI 側で、人は切替を入れておくだけ。
    ("auto_diag", "診断の自動応答"),
    # リセットの合図 (1-52): AI の発言の中の目印の行を見つけたら
    # 開き直す。通知ではなく動作なので、既定は他の切替に合わせて
    # 切 (sanitize_automation の通常の倒し方)。
    ("auto_reset", "合図で自動で開き直す"),
    # 再実行の合図 (1.63): AI の発言の中の目印の行 (コマンド ID
    # つき) を見つけたら、その ID のスクリプトを再実行して結果
    # を差分で返す。実行そのものなので、既定は切。
    ("auto_rerun", "合図で自動で再実行する"),
    # 結果再送の合図 (1.75): AI の発言の中の目印の行 (コマンド
    # ID つき) を見つけたら、その ID の直近の結果を投稿し直す。
    # 再実行と違い、実行は伴わない。既定は切。
    ("auto_resend", "合図で結果を自動で再送する"),
    ("auto_settings", "合図で設定を自動で知らせる"),
    ("auto_manual", "合図でマニュアルを自動で再送する"),
    ("auto_file", "合図でファイルを自動で送る"),
    # キャプチャの合図 (2.54): AI の発言の中の目印の行 (依頼 ID
    # と対象つき) を見つけたら、対象の窓の画面を PNG で撮って
    # 合図が出た窓へ添付して送る。画面そのものを送るので、
    # 既定は他の切替に合わせて切。
    ("auto_capture", "合図で画面を自動で撮って送る"),
    ("auto_fulltext", "全文の自動返信（要約の全文ファイル）"),
    # 実行一覧の合図 (1.92): AI の発言の中の目印の単独行を見つけ
    # たら、直近の実行一覧 (結果の控えの1行目だけ) を送る。
    # 実行は伴わない。既定は切。
    ("auto_history", "実行一覧の自動返信"),
    # 停止の合図 (1.93): AI の発言の中の目印の単独行を見つけ
    # たら、実行中のスクリプトを止める。実行中でなければ、
    # その旨を文で返す。既定は切。
    ("auto_stop", "停止の自動応答（実行中を止める）"),
    # 動作ログの合図 (1.96): AI の発言の中の目印の行 (指定
    # つき) を見つけたら、動作ログ (ビューアと同じ中身) を
    # 送る。件数・日付・時刻範囲の指定ができる。既定は切。
    ("auto_log", "動作ログの自動返信"),
    # ツール実行の合図 (1.98): AI の発言の中の目印の行
    # (「PHAL:ツール実行 tshark ...」) を見つけたら、許可
    # リスト (cli_tools) にあるコマンドラインツールを作業
    # フォルダで実行して結果を送る。実行そのものなので、
    # 既定は切。
    ("auto_tool", "合図でコマンドラインツールを自動で実行する"),
    # 中継リクエストの合図 (2.10、経路は 2.17): AI の発言の
    # 中の目印の単独行を見つけたら、次の行から発言の末尾まで
    # を別の AI のチャットへ送る。経路は目印の引数か、見張り
    # が2つのときの「もう一方」。既定は切。
    ("auto_relay", "合図で別のAIへ自動で中継する"),
    # 道具説明の合図 (2.37): AI の発言の中の目印の行 (依頼 ID
    # と道具名つき) を見つけたら、おどうぐばこの道具の取説
    # (SKILL.md) を送る。実行は伴わない。既定は切。
    ("auto_describe", "合図で道具の取説を自動で送る"),
    # 道具評価の合図 (2.37): AI の発言の中の目印の行 (道具名と
    # 5段階の点つき) を見つけたら、おどうぐばこの頻度の記録へ
    # 評価を足す。既定は切。
    ("auto_eval", "合図で道具の評価を自動で受け付ける"),
    # 入力欄リクエストの合図 (2.39): AI の発言の中の目印の行
    # (依頼 ID と任意の窓名つき) を見つけたら、対象の窓の
    # 入力欄に今入っている文をそのまま返す。実行は伴わない。
    # 既定は切。
    ("auto_input", "合図で入力欄の中身を自動で返す"),
    # 入力上書き・強制送信の合図 (2.39): AI の発言の先頭行の
    # 目印を見つけたら、目印の次の行から発言の末尾までを、
    # 対象の窓の入力欄へ強制的に上書きする (強制送信は送信
    # まで)。通常の書き込みが反映されないときの打鍵まで
    # 含む。既定は切。
    ("auto_force", "合図で入力欄へ強制的に上書き・送信する"),
)
AUTO_KEYS = tuple(key for key, _label in AUTO_LABELS)

# テスト入力の文 (3行)。入力欄に入れるだけで、送信はしない。
TEST_TEXT = (
    "PHAL からのテストです。\n"
    "これは送信されません。入力欄に入るかだけを確かめています。\n"
    "改行がそのまま入っていれば成功です。"
)

# 添付方式 (1.61)。プロファイルごとに持つ (target の attach_method)。
# Muse 以外の AI では、貼り付けとダイアログのどちらが効くか
# 分からないので、プロファイルごとに切り替えられるようにする。
# auto は、貼り付けで失敗したファイルだけダイアログでも試す。
ATTACH_METHOD_LABELS = (
    ("auto", "自動"),
    ("paste", "貼り付け"),
    ("dialog", "ダイアログ"),
)
ATTACH_METHOD_KEYS = tuple(key for key, _label in ATTACH_METHOD_LABELS)


def sanitize_zip_as_txt(value) -> bool:
    """ZIP 添付を .zip.txt に変えて渡すか (1.81、プロファイル
    ごと)。真とみなせる値だけを True にする。"""
    return value is True or value == "true"


def sanitize_attach_method(value) -> str:
    """添付方式を整える。auto / paste / dialog 以外は auto に倒す。"""
    return value if value in ATTACH_METHOD_KEYS else "auto"


# 入力・送信方式 (2.59)。プロファイルごとに持つ (target の
# send_method)。方式1 は従来 (書き込みの読み直しの一致で成否を
# 判定)。方式2 は、書き込みでは止めず、送信の段で入力欄の文が
# 想定どおりかを確認してから送信ボタンを押す (職場の Copilot
# で、文は入っているのに読み直しが一致せず止まる事故への対処)。
# 方式4 (2.68) は方式3 の判定に加え、書き込みの確認を直接読み
# にし、送信は読み直しが安定して大部分が合えば制限時間を
# 待たずに押す (環境2 で回答→送信が遅い問題への対処)。
SEND_METHOD_LABELS = (
    (1, "方式1（従来）"),
    (2, "方式2（送信時に文を確認）"),
    (3, "方式3（文を確認＋大部分が合えば送信）"),
    (4, "方式4（大部分が合えば即送信・高速）"),
)
SEND_METHOD_KEYS = tuple(key for key, _label in SEND_METHOD_LABELS)


def sanitize_send_method(value) -> int:
    """入力・送信方式を整える。2・3・4 以外は 1 (従来) に倒す。"""
    if value == 4 or value == "4":
        return 4
    if value == 3 or value == "3":
        return 3
    if value == 2 or value == "2":
        return 2
    return 1


# 添付テスト用のファイル (outbox へ書く)。送信はされない。
ATTACH_TEST_FILE_NAME = "phal-attach-test.txt"
ATTACH_TEST_TEXT = (
    "PHAL の添付テスト用のファイルです。\n"
    "設定画面の「添付テスト」で作られました。送信はされません。\n"
)

# カスタムの欄に無い項目の初期値 (copilot_loop の組み込みと桁を
# そろえた無難な値。editor/ai_target と同じ判断)。
_DEFAULT_RENDER_CLASS = "Chrome_RenderWidgetHostHWND"
_DEFAULT_SEND_BUTTONS = ["送信", "Send", "メッセージの送信"]
_DEFAULT_BUSY_BUTTONS = ["停止", "Stop", "生成を停止する"]
_DEFAULT_INPUT_BAND_PX = 170


def sanitize_fields(value) -> dict:
    """保存された投げ先の欄を整える。FIELD_KEYS の文字列だけ残す。"""
    value = value if isinstance(value, dict) else {}
    fields = {}
    for key in FIELD_KEYS:
        item = value.get(key)
        fields[key] = item.strip() if isinstance(item, str) else ""
    return fields


def is_configured(fields) -> bool:
    """窓を特定できる情報が1つでもあるか。

    全部空のプロファイルは「最初の窓なら何でも」になってしまう
    ので、設定済みとはみなさない (EDDy の保存検証と同じ)。"""
    fields = sanitize_fields(fields)
    return bool(fields["process_name"] or fields["window_class"]
                or fields["app_id_prefix"])


def send_configured(fields) -> bool:
    """送信ボタンの目印が1つでもあるか。

    行のボタンが「送信」の役割まで進んでよいかの判定 (script_runner
    が使う)。目印が無ければ、回答を入れても「AIへ回答」のままに
    する (押しようが無いボタンを出さない)。"""
    fields = sanitize_fields(fields)
    return bool(fields["send_automation_id"] or fields["send_name"]
                or fields["send_class"])


def sanitize_automation(value) -> dict:
    """自動化の切替を整える。{auto_dl, auto_run, auto_answer,
    auto_send, auto_diag, auto_ack, auto_reset, auto_rerun,
    auto_resend} の
    真偽だけの辞書を返す。

    value は settings.json の script_runner 節そのもの (キーは節の
    直下) か、切替だけの辞書。True のものだけ True で、それ以外
    (欠け・文字列など) は全部 False (手動) に倒す。自動化は
    既定で切るのが安全側。

    例外は auto_ack (受領応答) で、既定は入 (True)。受領は実行
    そのものではなく知らせで、自動実行 (auto_run) が入っている
    場面でのみ出るので、明示的に False のときだけ切る (欠け・
    壊れた値は入に倒す)。"""
    value = value if isinstance(value, dict) else {}
    result = {key: value.get(key) is True for key in AUTO_KEYS}
    ack = value.get("auto_ack")
    result["auto_ack"] = ack if isinstance(ack, bool) else True
    return result


def sanitize_dual_watch(section) -> dict:
    """二重待受の設定を整える (2.08)。

    script_runner 節の ai_dual_watch (真偽) と
    ai_secondary_profile (副のプロファイル名) を読む。入っている
    と明示されたときだけ入にし、名前は文字列でなければ空にする
    (壊れた値は「副は自動で選ぶ」に倒す)。"""
    section = section if isinstance(section, dict) else {}
    name = section.get("ai_secondary_profile")
    return {
        "enabled": section.get("ai_dual_watch") is True,
        "secondary": name.strip() if isinstance(name, str) else "",
    }


def sanitize_watch_profiles(section) -> list:
    """見張るプロファイルの名前の並び (2.20)。

    主・副の区別をなくし、見張る窓は名前の集合で持つ。
    新キー ai_watch_profiles (名前のリスト) があればそれを
    正とし、無ければ旧キー (アクティブなプロファイル +
    二重待受が入っていれば副) から組み立てる (移行は読み
    取りのたびに導く形で、設定ファイルは触らない)。並びは
    プロファイル一覧の順にそろえ、一覧に無い名前は落とす。
    空のリストは「見張りなし」としてそのまま受ける。"""
    section = section if isinstance(section, dict) else {}
    profiles, active = sanitize_profiles(section)
    names = [entry["name"] for entry in profiles]
    raw = section.get("ai_watch_profiles")
    if isinstance(raw, list):
        wanted = {value.strip() for value in raw
                  if isinstance(value, str)}
        return [name for name in names if name in wanted]
    if not names:
        return []
    picked = {names[active]}
    dual = sanitize_dual_watch(section)
    if dual["enabled"] and len(names) > 1:
        secondary = ""
        for index, name in enumerate(names):
            if index != active and name == dual["secondary"]:
                secondary = name
                break
        if not secondary:
            for index, name in enumerate(names):
                if index != active:
                    secondary = name
                    break
        if secondary:
            picked.add(secondary)
    return [name for name in names if name in picked]


# ---------------------------------------------------------------
# 設定のプロファイル (名前つきで複数持ち、切り替える)
# ---------------------------------------------------------------
# script_runner 節の形:
#   ai_profiles       … [{"name": 名前, "target": 欄の辞書}, ...]
#   ai_profile_active … 有効なプロファイルの位置 (読み取りでは
#                       名前でも受け付ける)
#   ai_target         … 有効なプロファイルの欄の写し。保存のたびに
#                       同期するので、旧キーだけを読む側もそのまま
#                       動く (読み取りの正本は ai_profiles 側)。
DEFAULT_PROFILE_NAME = "既定"


def _clean_profile_name(value) -> str:
    name = value.strip() if isinstance(value, str) else ""
    return name or DEFAULT_PROFILE_NAME


def _profile_entry(name, raw_target, fallback_source=None) -> dict:
    target = raw_target if isinstance(raw_target, dict) else fallback_source
    cleaned = sanitize_fields(target)
    # 添付方式 (1.61) は FIELD_KEYS の文字列欄ではないが、プロファイル
    # (target) の一部として一緒に持つ。sanitize_fields では落ちる
    # ので、ここで整えて足し直す。
    source = target if isinstance(target, dict) else {}
    cleaned["attach_method"] = sanitize_attach_method(
        source.get("attach_method"))
    cleaned["zip_as_txt"] = sanitize_zip_as_txt(
        source.get("zip_as_txt"))
    cleaned["send_method"] = sanitize_send_method(
        source.get("send_method"))
    return {"name": _clean_profile_name(name),
            "target": cleaned}


def sanitize_profiles(section):
    """script_runner 節から (プロファイルの並び, 有効な位置) を出す。

    プロファイルは {"name": 名前, "target": 欄の辞書} の形に整える。
    ai_profiles が無ければ、旧 ai_target を「既定」1件に包む (移行)。
    どちらも無ければ空の「既定」1件。有効な位置は
    ai_profile_active (位置か名前) から決め、当てはまらなければ
    先頭にする。"""
    section = section if isinstance(section, dict) else {}
    profiles = []
    raw_profiles = section.get("ai_profiles")
    if isinstance(raw_profiles, list):
        for entry in raw_profiles:
            if not isinstance(entry, dict):
                continue
            profiles.append(_profile_entry(
                entry.get("name"), entry.get("target"), entry))
    if not profiles:
        profiles = [_profile_entry(
            DEFAULT_PROFILE_NAME, section.get("ai_target"))]
    active = 0
    raw_active = section.get("ai_profile_active")
    if isinstance(raw_active, bool):
        raw_active = None
    if isinstance(raw_active, int):
        active = min(max(raw_active, 0), len(profiles) - 1)
    elif isinstance(raw_active, str):
        wanted = raw_active.strip()
        for index, profile in enumerate(profiles):
            if profile["name"] == wanted:
                active = index
                break
    return profiles, active


def active_profile_fields(section) -> dict:
    """有効なプロファイルの欄 (読み取りの入口)。

    回答・送信・チャットの見張りは、ここを通して「今選ばれている
    投げ先」の欄だけを見る。"""
    profiles, active = sanitize_profiles(section)
    return profiles[active]["target"]


def profiles_settings(profiles, active_index) -> dict:
    """保存用の辞書 {ai_profiles, ai_profile_active, ai_target} を作る。

    ai_target には有効なプロファイルの欄を写す (旧キーだけを読む側
    がそのまま動くように、保存のたび同期する)。"""
    cleaned = []
    for entry in profiles if isinstance(profiles, list) else []:
        if not isinstance(entry, dict):
            continue
        cleaned.append(_profile_entry(
            entry.get("name"), entry.get("target"), entry))
    if not cleaned:
        cleaned = [_profile_entry(DEFAULT_PROFILE_NAME, None)]
    if isinstance(active_index, bool) \
            or not isinstance(active_index, int):
        active_index = 0
    active = min(max(active_index, 0), len(cleaned) - 1)
    return {
        "ai_profiles": cleaned,
        "ai_profile_active": active,
        "ai_target": cleaned[active]["target"],
    }


def build_profile(fields) -> dict:
    """欄の値から、copilot_io へ渡すプロファイル辞書を組み立てる。

    形は copilot_loop のプロファイルにそろえる。欄に無い項目は
    初期値を入れ、応答の切り出し用マーカーは空にしておく (PHAL は
    回答を送る側で、切り出しはしない。当てずっぽうで埋めると
    誤動作の元になるため)。添付方式 (1.61) は欄の外の項目だが、
    copilot_loop が self.profile から読むので、ここで一緒に渡す。"""
    attach_method = sanitize_attach_method(
        fields.get("attach_method") if isinstance(fields, dict)
        else None)
    # ZIP の名前変え (1.81) も欄の外の項目。sanitize_fields で
    # 落ちるので、attach_method と同じく先に取り出す (1.84 で
    # 修正: 後から読むと常に False になっていた)。
    zip_as_txt = sanitize_zip_as_txt(
        fields.get("zip_as_txt") if isinstance(fields, dict)
        else None)
    # 入力・送信方式 (2.59) も欄の外の項目。attach_method と
    # 同じく、先に取り出す。
    send_method = sanitize_send_method(
        fields.get("send_method") if isinstance(fields, dict)
        else None)
    fields = sanitize_fields(fields)
    return {
        "name": PROFILE_NAME,
        "label": fields["label"],
        "process_name": fields["process_name"],
        "window_class": fields["window_class"],
        "window_title_contains": fields["window_title_contains"],
        "app_id_prefix": fields["app_id_prefix"],
        "render_class": _DEFAULT_RENDER_CLASS,
        "input_automation_id": fields["input_automation_id"],
        "input_class": fields["input_class"],
        "input_name": fields["input_name"],
        # 送信ボタンの目印 (PHAL が「送信」まで回すための3点)。
        # copilot_loop の組み込みが使う send_button (名前の並び) とは
        # 別物で、press_send_button がこちらだけを見る。
        "send_automation_id": fields["send_automation_id"],
        "send_name": fields["send_name"],
        "send_class": fields["send_class"],
        "send_button": list(_DEFAULT_SEND_BUTTONS),
        "busy_button": list(_DEFAULT_BUSY_BUTTONS),
        "assistant_marker": "",
        "user_marker": "",
        "idle_marker": [],
        "code_copy_button": [],
        "input_band_px": _DEFAULT_INPUT_BAND_PX,
        # 添付方式 (1.61)。copilot_loop の振り分けが読む。
        "attach_method": attach_method,
        # ZIP 添付を .zip.txt に変えて渡すか (1.81)。
        "zip_as_txt": zip_as_txt,
        # 入力・送信方式 (2.59)。script_runner の回答・送信が読む。
        "send_method": send_method,
    }


def fields_from_window_row(row) -> dict:
    """窓の一覧の1行 {title, process_name, window_class} から、
    欄へ写すぶんを作る (editor の投げ先画面と同じ作法)。

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


def candidate_text(candidate) -> str:
    """調査の候補1件を、人が選べる形の1行にする。"""
    candidate = candidate if isinstance(candidate, dict) else {}

    def show(value):
        return value if isinstance(value, str) and value else "(なし)"

    return (
        f"{candidate.get('control_type') or '?'}"
        f" / 名前: {show(candidate.get('name'))}"
        f" / AutomationId: {show(candidate.get('automation_id'))}"
        f" / クラス: {show(candidate.get('class_name'))}"
        f" / 値の書き込み: "
        f"{'可' if candidate.get('value_pattern') else '不可'}"
        f" / テキスト: "
        f"{'あり' if candidate.get('text_pattern') else 'なし'}")


def button_candidate_text(candidate) -> str:
    """送信ボタンの候補1件を、人が選べる形の1行にする。

    入力欄の候補と並びはそろえ、最後の段だけ「押せるか
    (InvokePattern)」に替える (ボタンに値の書き込みは関係ない)。"""
    candidate = candidate if isinstance(candidate, dict) else {}

    def show(value):
        return value if isinstance(value, str) and value else "(なし)"

    return (
        f"{candidate.get('control_type') or '?'}"
        f" / 名前: {show(candidate.get('name'))}"
        f" / AutomationId: {show(candidate.get('automation_id'))}"
        f" / クラス: {show(candidate.get('class_name'))}"
        f" / 押下: "
        f"{'可' if candidate.get('invoke_pattern') else '不可'}")


# ---------------------------------------------------------------
# 窓の一覧 (素の Win32 列挙。UIA は使わない)
# ---------------------------------------------------------------
try:
    # 専用の WinDLL を持つ (ctypes.windll の共有オブジェクトは
    # 他モジュールと署名の取り合いになるため。copilot_loop と同じ)。
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
except (AttributeError, OSError):
    # Windows 以外では WinDLL 自体が無い。一覧は空で倒す。
    _user32 = None

if _user32 is not None:
    _user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.GetWindowTextW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_ulong


def _title_of(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _exe_of(hwnd):
    """窓を持っているプロセスの実行ファイル名。取れなければ ''。"""
    try:
        import psutil

        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001
        return ""


def enumerate_visible_windows() -> list:
    """見えているトップレベルの窓を [{title, window_class,
    process_name}] で返す。Windows でなければ空。

    UIA は使わない (素の Win32 列挙)。タイトルの無い窓は、
    選んでも手掛かりにならないので除く。"""
    if _user32 is None:
        return []
    rows = []
    enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on_window(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _title_of(hwnd)
            if not title:
                return True
            rows.append({
                "title": title,
                "window_class": _class_of(hwnd),
                "process_name": _exe_of(hwnd),
            })
        except Exception:  # noqa: BLE001  1つの窓で全体を止めない
            pass
        return True

    callback = enum_proc(on_window)
    _user32.EnumWindows(callback, None)
    return rows


def _default_scanner(profile):
    """調査の既定の実体 (copilot_io の裏方経由)。"""
    import copilot_io

    return copilot_io.scan_input(profile)


def _default_filler(text, profile):
    """テスト入力の既定の実体 (copilot_io の裏方経由)。"""
    import copilot_io

    return copilot_io.fill_text(text, profile=profile)


def _default_button_scanner(profile):
    """送信ボタン調査の既定の実体 (copilot_io の裏方経由)。"""
    import copilot_io

    return copilot_io.scan_send_buttons(profile)


def _default_attach_tester(path, profile):
    """添付テストの既定の実体 (copilot_io の裏方経由)。"""
    import copilot_io

    return copilot_io.attach_test(path, profile=profile)


class _OpBridge(QObject):
    """裏スレッドの調査・テスト入力 → 画面への結果の受け渡し。"""

    finished = Signal(object)


class WindowPickerDialog(QDialog):
    """窓の一覧から1つ選ぶ小窓。rows は enumerate_visible_windows
    の形 ({title, window_class, process_name} の並び)。"""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("投げ先の窓を選ぶ - PHAL")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        # 絞り込み。打つそばから、行の表示文 (タイトル / プロセス名 /
        # クラス) に当てはまる行だけ残す。大文字小文字は区別せず、
        # 空白で区切った語は全部含む行だけ (AND)。空なら全行。
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(
            "絞り込み (タイトル・プロセス名・クラス名の一部)")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter_edit)
        self._total = len(rows)
        self._all_text = (
            f"{len(rows)} 件の窓があります" if rows
            else "選べる窓がありません")
        self._status_label = QLabel(self._all_text)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._list = QListWidget()
        for row in rows:
            text = row.get("title") or ""
            detail = " / ".join(
                part for part in (row.get("process_name") or "",
                                  row.get("window_class") or "") if part)
            item = QListWidgetItem(f"{text}（{detail}）" if detail else text)
            item.setData(Qt.ItemDataRole.UserRole, row)
            self._list.addItem(item)
        self._list.itemActivated.connect(lambda _item: self.accept())
        layout.addWidget(self._list, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        # 開いたらすぐ打てるよう、絞り込み欄にフォーカスを置く。
        self._filter_edit.setFocus()

    def _apply_filter(self, text):
        terms = [term.casefold() for term in text.split() if term]
        visible = 0
        for index in range(self._list.count()):
            item = self._list.item(index)
            haystack = item.text().casefold()
            matched = all(term in haystack for term in terms)
            item.setHidden(not matched)
            if matched:
                visible += 1
        if terms:
            self._status_label.setText(
                f"{visible} 件が当てはまります (全 {self._total} 件)")
        else:
            self._status_label.setText(self._all_text)
        current = self._list.currentItem()
        if current is not None and current.isHidden():
            # 選んでいる行が隠れたら、見えている先頭へ移す
            # (無ければ選択なし)。隠れた行が返ることはない。
            self._list.setCurrentItem(None)
            for index in range(self._list.count()):
                item = self._list.item(index)
                if not item.isHidden():
                    self._list.setCurrentItem(item)
                    break

    def selected_row(self):
        item = self._list.currentItem()
        if item is None:
            return None
        row = item.data(Qt.ItemDataRole.UserRole)
        return row if isinstance(row, dict) else None


def _copy_all_button(list_widget, status_label, heading):
    """候補の一覧を全部クリップボードへ入れるボタン。

    調査の結果は画面に収まらないことがある (Web の窓はボタンが
    大量にある)。結果をそのまま AI へ貼って見せられるようにする。
    """
    button = QPushButton("一覧をコピー")

    def _copy():
        lines = [heading]
        for row in range(list_widget.count()):
            lines.append(list_widget.item(row).text())
        QApplication.clipboard().setText("\n".join(lines))
        status_label.setText(f"{status_label.text()}  一覧をコピーしました。")

    button.clicked.connect(_copy)
    return button


class InputCandidateDialog(QDialog):
    """調査で拾った入力欄の候補から1つ選ぶ小窓。"""

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("入力欄の候補 - PHAL")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        self._status_label = QLabel(
            f"候補が {len(candidates)} 件あります。"
            "入力欄だと思うものを選んでください")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._list = QListWidget()
        for candidate in candidates:
            item = QListWidgetItem(candidate_text(candidate))
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self._list.addItem(item)
        if candidates:
            self._list.setCurrentRow(0)
        self._list.itemActivated.connect(lambda _item: self.accept())
        layout.addWidget(self._list, 1)
        layout.addWidget(_copy_all_button(
            self._list, self._status_label, "入力欄の候補 - PHAL"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_candidate(self):
        item = self._list.currentItem()
        if item is None:
            return None
        candidate = item.data(Qt.ItemDataRole.UserRole)
        return candidate if isinstance(candidate, dict) else None


class ButtonCandidateDialog(QDialog):
    """調査で拾った送信ボタンの候補から1つ選ぶ小窓。

    入力欄の候補の小窓 (InputCandidateDialog) と同じ形で、行の文だけ
    ボタン用 (button_candidate_text) に替えたもの。"""

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("送信ボタンの候補 - PHAL")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        self._status_label = QLabel(
            f"候補が {len(candidates)} 件あります。"
            "送信ボタンだと思うものを選んでください")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._list = QListWidget()
        for candidate in candidates:
            item = QListWidgetItem(button_candidate_text(candidate))
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self._list.addItem(item)
        if candidates:
            self._list.setCurrentRow(0)
        self._list.itemActivated.connect(lambda _item: self.accept())
        layout.addWidget(self._list, 1)
        layout.addWidget(_copy_all_button(
            self._list, self._status_label, "送信ボタンの候補 - PHAL"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_candidate(self):
        item = self._list.currentItem()
        if item is None:
            return None
        candidate = item.data(Qt.ItemDataRole.UserRole)
        return candidate if isinstance(candidate, dict) else None


class LogViewerPage(QWidget):
    """動作ログのビューア (1.90、2.01 でタブのページ化)。

    動作ログ (script_runner_log.json、開き直しをまたいで上限
    1000件) を日付つきで古い順に並べて見せる。読み込み直しと
    全文コピーができる。見るだけの部品で、ログ自体は変えない。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        layout.addWidget(self._view)
        row = QHBoxLayout()
        reload_button = QPushButton("再読み込み")
        reload_button.clicked.connect(self.refresh)
        row.addWidget(reload_button)
        copy_button = QPushButton("コピー")
        copy_button.clicked.connect(self._copy_all)
        row.addWidget(copy_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh()

    def refresh(self):
        """置き場から読み直して表示する (末尾 = 直近に寄せる)。"""
        import script_runner_core as core
        events = core.load_log_events()
        lines = core.format_log_lines(events, with_date=True)
        self._view.setPlainText(
            "\n".join(lines) if lines else "(記録はまだありません)")
        cursor = self._view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._view.setTextCursor(cursor)

    def _copy_all(self):
        QApplication.clipboard().setText(self._view.toPlainText())


class LogViewerDialog(QDialog):
    """動作ログの単独窓 (LogViewerPage の包み)。

    設定のタブ化 (2.01) で普段の入口はタブになったが、単独で
    開く使い方のために残してある。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("動作ログ")
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        self._page = LogViewerPage(self)
        self._view = self._page._view
        layout.addWidget(self._page)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

    def refresh(self):
        self._page.refresh()


class AiProfilePage(QWidget):
    """「AI別設定」タブのページ (投げ先プロファイル)。

    旧 AiWindowDialog のプロファイル部分 (2.01 でタブ化)。
    enumerate_windows / scanner / filler / button_scanner /
    attach_tester は差し替え口で、scanner・button_scanner(profile)
    は .ok / .message / .candidates、filler(text, profile) は
    .ok / .message / .needs_confirm を持つ結果を返し、
    attach_tester(path, profile) は .ok / .message を持つ結果を
    返す (copilot_io の ScanResult / SendResult)。調査とテスト
    入力は数秒かかることがあるので、裏のスレッドで回して橋で受け取る
    (EDDy の投げ先画面と同じ作法)。"""

    def __init__(self, fields, parent=None, *, enumerate_windows=None,
                 scanner=None, filler=None, button_scanner=None,
                 attach_tester=None,
                 profiles=None, active_index=0,
                 diag_text_provider=None, error_color="#c00000",
                 watch_names=None):
        super().__init__(parent)
        # 見張る窓の初期値 (2.20、旧二重待受)。名前の集合で
        # 保持だけする (この画面では変えない、2.22)。
        self._watch_names = {str(name) for name in watch_names or []}
        # 状態ラベルのエラー色。窓側がテーマに合わせて渡す (1-44)。
        # 渡されなければ従来の色のまま。
        self._error_color = error_color
        self._enumerate_windows = (
            enumerate_windows or enumerate_visible_windows)
        self._scanner = scanner or _default_scanner
        self._filler = filler or _default_filler
        self._button_scanner = button_scanner or _default_button_scanner
        self._attach_tester = attach_tester or _default_attach_tester
        # 診断の束を作る役 (窓側が渡す)。無ければ「診断をAIへ貼る」
        # ボタンは出さない。
        self._diag_text_provider = diag_text_provider
        self._bridge = _OpBridge(self)
        self._bridge.finished.connect(self._on_op_finished)
        self._op_thread = None
        self._op_done = None

        # プロファイル (名前つきの投げ先)。渡されなければ fields を
        # 「既定」1件として扱う (従来の呼び方との互換)。
        self._profiles = []
        for entry in profiles if isinstance(profiles, list) else []:
            if isinstance(entry, dict):
                self._profiles.append(_profile_entry(
                    entry.get("name"), entry.get("target"), entry))
        if not self._profiles:
            self._profiles = [_profile_entry(
                DEFAULT_PROFILE_NAME, fields)]
        if isinstance(active_index, bool) \
                or not isinstance(active_index, int):
            active_index = 0
        self._current = min(
            max(active_index, 0), len(self._profiles) - 1)

        layout = QVBoxLayout(self)

        # プロファイルの切り替え行 (一番上)。切り替えると欄の表示が
        # そのプロファイルの内容に替わる。編集中の欄は、切り替える
        # 前に今のプロファイルへ控えておく (黙って消さない)。
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("設定"))
        self._profile_combo = QComboBox()
        self._profile_combo.setToolTip(
            "選んだ設定が「既定の投げ先」になります"
            "（開始プロンプトなど、送り先の決まらない操作の"
            "宛先。2.21）")
        self._profile_combo.currentIndexChanged.connect(
            self._on_profile_switched)
        profile_row.addWidget(self._profile_combo, 1)
        self._add_profile_button = QPushButton("追加")
        self._add_profile_button.clicked.connect(self._add_profile)
        self._dup_profile_button = QPushButton("複写")
        self._dup_profile_button.clicked.connect(self._duplicate_profile)
        self._rename_profile_button = QPushButton("名前変更")
        self._rename_profile_button.clicked.connect(self._rename_profile)
        self._delete_profile_button = QPushButton("削除")
        self._delete_profile_button.clicked.connect(self._delete_profile)
        for button in (self._add_profile_button, self._dup_profile_button,
                       self._rename_profile_button,
                       self._delete_profile_button):
            profile_row.addWidget(button)
        layout.addLayout(profile_row)
        self._rebuild_profile_combo()

        form = QFormLayout()
        self._edits = {}
        for key, label in FIELD_LABELS:
            edit = QLineEdit()
            self._edits[key] = edit
            form.addRow(label, edit)
        # 添付方式 (1.61)。文字列の欄ではないので、欄の並びの
        # 末尾に選択として足す。プロファイルごとに保存される。
        self._attach_method_combo = QComboBox()
        for key, label in ATTACH_METHOD_LABELS:
            self._attach_method_combo.addItem(label, key)
        form.addRow("添付方式", self._attach_method_combo)
        # 入力・送信方式 (2.59)。添付方式と同じく選択で足す。
        self._send_method_combo = QComboBox()
        for key, label in SEND_METHOD_LABELS:
            self._send_method_combo.addItem(label, key)
        form.addRow("入力・送信方式", self._send_method_combo)
        layout.addLayout(form)
        # ZIP 添付を .zip.txt に変えて渡す (1.81、プロファイル
        # ごと。ユーザー → Copilot の向きは ZIP をそのまま
        # 渡せない相手向け)。
        self._zip_as_txt_box = QCheckBox(
            "ZIP 添付を .zip.txt に変えて渡す")
        layout.addWidget(self._zip_as_txt_box)
        # 見張る窓の集合 (2.20) はこの画面では変えない (変える
        # 場所は PHAL 本体の階のチェックだけ、2.22)。ここでは
        # 受け取った集合を保持し、プロファイルの名前変更・
        # 削除に追随させるだけ (watch_names で窓側へ返す)。

        row = QHBoxLayout()
        self._pick_button = QPushButton("窓の一覧…")
        self._pick_button.clicked.connect(self._pick_window)
        self._scan_button = QPushButton("入力欄を調査")
        self._scan_button.clicked.connect(self._scan_inputs)
        self._scan_send_button = QPushButton("送信ボタンを調査")
        self._scan_send_button.clicked.connect(self._scan_send_buttons)
        self._test_button = QPushButton("テスト入力")
        self._test_button.clicked.connect(self._test_fill)
        self._attach_test_button = QPushButton("添付テスト")
        self._attach_test_button.clicked.connect(self._test_attach)
        for button in (self._pick_button, self._scan_button,
                       self._scan_send_button, self._test_button,
                       self._attach_test_button):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        # 診断の行: PHAL の直近の動きの記録を、表示中のプロファイル
        # の入力欄へ入れる (入れるだけ。送信は人が押す。テスト入力
        # と同じ線引き)。ループが止まったとき、その理由を AI へ
        # そのまま渡すためのもの (1-41)。
        diag_row = QHBoxLayout()
        if self._diag_text_provider is not None:
            self._diag_button = QPushButton("診断をAIへ貼る")
            self._diag_button.clicked.connect(self._fill_diagnostics)
            diag_row.addWidget(self._diag_button)
        else:
            self._diag_button = None
        diag_row.addStretch(1)
        layout.addLayout(diag_row)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        layout.addStretch(1)

        self._set_fields(self._profiles[self._current]["target"])

    # -- プロファイルまわり ---------------------------------------------
    def _rebuild_profile_combo(self):
        """プルダウンをプロファイルの並びから作り直す。

        作り直しの間の currentIndexChanged は止める (切り替え扱いに
        なって編集中の欄を上書きしないように)。"""
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for profile in self._profiles:
            self._profile_combo.addItem(profile["name"])
        self._profile_combo.setCurrentIndex(self._current)
        self._profile_combo.blockSignals(False)
        self._sync_profile_buttons()

    def _sync_profile_buttons(self):
        # 最後の1件は消せない (投げ先が必ず1つは要る)。
        self._delete_profile_button.setEnabled(len(self._profiles) > 1)

    def _stash_current(self):
        """編集中の欄を、今のプロファイルへ控える。"""
        self._profiles[self._current]["target"] = self.fields()

    def _on_profile_switched(self, index):
        if index < 0 or index == self._current \
                or index >= len(self._profiles):
            return
        self._stash_current()
        self._current = index
        self._set_fields(self._profiles[index]["target"])
        self._show_status(
            f"設定「{self._profiles[index]['name']}」に切り替えました")

    def profiles(self) -> list:
        """全プロファイル (編集中の欄は今のものへ控えたうえで)。"""
        self._stash_current()
        return [_profile_entry(p["name"], p["target"])
                for p in self._profiles]

    def active_index(self) -> int:
        """今選ばれているプロファイルの位置。"""
        return self._current

    def watch_names(self) -> list:
        """見張る窓の名前の並び (プロファイルの順、2.20)。

        この画面では集合を変えず、受け取った集合をプロファイル
        の名前変更・削除に追随させた結果を返す (2.22)。"""
        return [entry["name"] for entry in self._profiles
                if entry["name"] in self._watch_names]

    def _ask_profile_name(self, title, initial):
        """名前を聞く。やめた・空のときは None (空は理由を出す)。"""
        text, ok = QInputDialog.getText(self, title, "名前:", text=initial)
        if not ok:
            return None
        name = text.strip()
        if not name:
            self._show_status("名前が空なので、やめました", error=True)
            return None
        return name

    def _add_profile(self):
        name = self._ask_profile_name("設定の追加", "")
        if name is None:
            return
        self._stash_current()
        self._profiles.append(_profile_entry(name, None))
        self._current = len(self._profiles) - 1
        self._rebuild_profile_combo()
        self._set_fields(self._profiles[self._current]["target"])
        self._show_status(f"設定「{name}」を追加しました")

    def _duplicate_profile(self):
        current_name = self._profiles[self._current]["name"]
        name = self._ask_profile_name(
            "設定の複写", f"{current_name} のコピー")
        if name is None:
            return
        self._stash_current()
        copied = _profile_entry(
            name, self._profiles[self._current]["target"])
        self._profiles.insert(self._current + 1, copied)
        self._current += 1
        self._rebuild_profile_combo()
        self._set_fields(copied["target"])
        self._show_status(f"設定「{name}」を複写しました")

    def _rename_profile(self):
        name = self._ask_profile_name(
            "名前変更", self._profiles[self._current]["name"])
        if name is None:
            return
        old = self._profiles[self._current]["name"]
        self._profiles[self._current]["name"] = name
        # 見張る窓の集合も名前で持つので、付け替える (2.20)。
        if old in self._watch_names:
            self._watch_names.discard(old)
            self._watch_names.add(name)
        self._rebuild_profile_combo()
        self._show_status(f"名前を「{name}」に変えました")

    def _delete_profile(self):
        if len(self._profiles) <= 1:
            return  # 最後の1件は消さない (ボタンも無効にしてある)
        name = self._profiles[self._current]["name"]
        self._watch_names.discard(name)
        del self._profiles[self._current]
        self._current = min(self._current, len(self._profiles) - 1)
        self._rebuild_profile_combo()
        self._set_fields(self._profiles[self._current]["target"])
        self._show_status(f"設定「{name}」を削除しました")

    # -- 欄まわり ------------------------------------------------------
    def _set_fields(self, fields):
        for key in FIELD_KEYS:
            self._edits[key].setText(fields.get(key) or "")
        method = sanitize_attach_method(fields.get("attach_method"))
        index = self._attach_method_combo.findData(method)
        self._attach_method_combo.setCurrentIndex(max(index, 0))
        send_method = sanitize_send_method(fields.get("send_method"))
        index = self._send_method_combo.findData(send_method)
        self._send_method_combo.setCurrentIndex(max(index, 0))
        self._zip_as_txt_box.setChecked(
            sanitize_zip_as_txt(fields.get("zip_as_txt")))

    def fields(self) -> dict:
        result = sanitize_fields(
            {key: self._edits[key].text() for key in FIELD_KEYS})
        result["attach_method"] = sanitize_attach_method(
            self._attach_method_combo.currentData())
        result["send_method"] = sanitize_send_method(
            self._send_method_combo.currentData())
        result["zip_as_txt"] = bool(self._zip_as_txt_box.isChecked())
        return result

    def _show_status(self, message, error=False):
        self._status_label.setStyleSheet(
            f"color: {self._error_color};" if error else "")
        self._status_label.setText(message)

    # -- 窓の一覧 --------------------------------------------------------
    def _pick_window(self):
        try:
            rows = self._enumerate_windows()
        except Exception:  # noqa: BLE001  一覧が取れないだけ
            rows = []
        picker = WindowPickerDialog(rows, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        row = picker.selected_row()
        if row is None:
            return
        fields = self.fields()
        picked = fields_from_window_row(row)
        if fields["label"]:  # 表示名は手で入れていればそちらを残す
            picked.pop("label", None)
        fields.update(picked)
        self._set_fields(fields)
        self._show_status(f"窓から取り込みました: {row.get('title') or ''}")

    # -- 調査とテスト入力 (裏のスレッド) --------------------------------
    def _op_widgets(self):
        """調査・テスト入力の間は止める部品 (押下系とプロファイル行)。"""
        widgets = [self._pick_button, self._scan_button,
                   self._scan_send_button, self._test_button,
                   self._attach_test_button,
                   self._profile_combo, self._add_profile_button,
                   self._dup_profile_button, self._rename_profile_button,
                   self._delete_profile_button]
        if self._diag_button is not None:
            widgets.append(self._diag_button)
        return tuple(widgets)

    def _start_op(self, fn, on_result):
        if self._op_thread is not None:
            return
        self._op_done = on_result
        for widget in self._op_widgets():
            widget.setEnabled(False)

        def run():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001  文にして画面へ戻す
                result = SimpleNamespace(
                    ok=False, message=f"失敗しました（{e}）",
                    candidates=[], needs_confirm=False)
            self._bridge.finished.emit(result)

        self._op_thread = threading.Thread(
            target=run, daemon=True, name="phal-ai-window-op")
        self._op_thread.start()

    def _on_op_finished(self, result):
        self._op_thread = None
        for widget in self._op_widgets():
            widget.setEnabled(True)
        # 削除ボタンは「1件だけなら無効」に戻す。
        self._sync_profile_buttons()
        on_result, self._op_done = self._op_done, None
        if on_result is not None:
            on_result(result)

    def _profile_or_none(self):
        """今の欄からプロファイルを作る。窓を特定できなければ
        None (理由は画面に出す)。"""
        fields = self.fields()
        if not is_configured(fields):
            self._show_status(
                "先に窓の情報が必要です。「窓の一覧…」から選ぶか、"
                "プロセス名・ウィンドウクラス・アプリIDの頭のどれかを"
                "入れてください", error=True)
            return None
        return build_profile(fields)

    def _scan_inputs(self):
        profile = self._profile_or_none()
        if profile is None:
            return
        self._show_status("入力欄を調査しています…")
        self._start_op(lambda: self._scanner(profile),
                       self._on_scan_result)

    def _on_scan_result(self, result):
        if not getattr(result, "ok", False):
            self._show_status(
                getattr(result, "message", "調査できませんでした"),
                error=True)
            return
        candidates = [c for c in (getattr(result, "candidates", None)
                                  or []) if isinstance(c, dict)]
        if not candidates:
            self._show_status("候補が見つかりませんでした", error=True)
            return
        picker = InputCandidateDialog(candidates, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            self._show_status(
                f"{result.message}（選びませんでした）")
            return
        candidate = picker.selected_candidate()
        if candidate is not None:
            self._apply_candidate(candidate)

    def _apply_candidate(self, candidate):
        aid = (candidate.get("automation_id") or "").strip()
        class_name = (candidate.get("class_name") or "").strip()
        name = (candidate.get("name") or "").strip()
        if aid:
            self._edits["input_automation_id"].setText(aid)
            self._edits["input_class"].setText("")
            self._edits["input_name"].setText("")
            message = f"入力欄は AutomationId「{aid}」で合わせます"
        elif class_name or name:
            self._edits["input_automation_id"].setText("")
            self._edits["input_class"].setText(class_name)
            self._edits["input_name"].setText(name)
            message = (
                "AutomationId が無いので、クラスと名前で合わせます"
                f"（クラス: {class_name or '(なし)'} / "
                f"名前: {name or '(なし)'}）")
        else:
            self._show_status(
                "その候補には、合わせる目印（AutomationId・クラス・"
                "名前）がありません", error=True)
            return
        if not candidate.get("value_pattern"):
            message += (
                "。ただし、この要素には値の書き込み（ValuePattern）"
                "が無いので、テスト入力が失敗する可能性があります")
        self._show_status(message)

    def _scan_send_buttons(self):
        profile = self._profile_or_none()
        if profile is None:
            return
        self._show_status("送信ボタンを調査しています…")
        self._start_op(lambda: self._button_scanner(profile),
                       self._on_button_scan_result)

    def _on_button_scan_result(self, result):
        if not getattr(result, "ok", False):
            self._show_status(
                getattr(result, "message", "調査できませんでした"),
                error=True)
            return
        candidates = [c for c in (getattr(result, "candidates", None)
                                  or []) if isinstance(c, dict)]
        if not candidates:
            self._show_status("候補が見つかりませんでした", error=True)
            return
        picker = ButtonCandidateDialog(candidates, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            self._show_status(
                f"{result.message}（選びませんでした）")
            return
        candidate = picker.selected_candidate()
        if candidate is not None:
            self._apply_button_candidate(candidate)

    def _apply_button_candidate(self, candidate):
        aid = (candidate.get("automation_id") or "").strip()
        class_name = (candidate.get("class_name") or "").strip()
        name = (candidate.get("name") or "").strip()
        if aid:
            self._edits["send_automation_id"].setText(aid)
            self._edits["send_class"].setText("")
            self._edits["send_name"].setText("")
            message = f"送信ボタンは AutomationId「{aid}」で合わせます"
        elif class_name or name:
            self._edits["send_automation_id"].setText("")
            self._edits["send_class"].setText(class_name)
            self._edits["send_name"].setText(name)
            message = (
                "AutomationId が無いので、クラスと名前で合わせます"
                f"（クラス: {class_name or '(なし)'} / "
                f"名前: {name or '(なし)'}）")
        else:
            self._show_status(
                "その候補には、合わせる目印（AutomationId・クラス・"
                "名前）がありません", error=True)
            return
        if not candidate.get("invoke_pattern"):
            message += (
                "。ただし、この要素には押す仕組み（InvokePattern）"
                "が無いので、押せない可能性があります")
        self._show_status(message)

    def _test_fill(self):
        profile = self._profile_or_none()
        if profile is None:
            return
        self._show_status("テスト文を入力欄に入れています…")
        self._start_op(
            lambda: self._filler(TEST_TEXT, profile),
            lambda result: self._show_status(
                getattr(result, "message", ""),
                error=not getattr(result, "ok", False)))

    def _test_attach(self):
        """添付テスト (1.61): テスト用ファイルを outbox へ書き、
        今の欄 (保存前でも現在の入力値) から作ったプロファイルの
        方式で添付してみる。送信はしない。

        テスト後は裏方側でチップを外す (外せなかったら、その旨も
        結果の文に含まれる)。結果は状態ラベルへ出す。"""
        profile = self._profile_or_none()
        if profile is None:
            return
        try:
            import script_runner_core as core

            outbox = core.outbox_dir()
            outbox.mkdir(parents=True, exist_ok=True)
            path = outbox / ATTACH_TEST_FILE_NAME
            path.write_text(ATTACH_TEST_TEXT, encoding="utf-8")
        except Exception as e:  # noqa: BLE001  書けなければテストできない
            self._show_status(
                f"テスト用ファイルを書けませんでした（{e}）",
                error=True)
            return
        self._show_status("添付テストをしています…")
        self._start_op(
            lambda: self._attach_tester(str(path), profile),
            lambda result: self._show_status(
                getattr(result, "message", ""),
                error=not getattr(result, "ok", False)))

    def _fill_diagnostics(self):
        """診断の束を、表示中のプロファイルの入力欄へ入れる。

        入れるだけ (送信は人が押す)。束は押した時点の状態で作る
        (窓側の diag_text_provider)。作れなければ理由を画面に出す。"""
        profile = self._profile_or_none()
        if profile is None:
            return
        try:
            text = self._diag_text_provider()
        except Exception as e:  # noqa: BLE001  文にして画面へ出す
            self._show_status(
                f"診断を組み立てられませんでした（{e}）", error=True)
            return
        if not text:
            self._show_status("診断がありません", error=True)
            return
        self._show_status("診断を入力欄に入れています…")
        self._start_op(
            lambda: self._filler(text, profile),
            lambda result: self._show_status(
                getattr(result, "message", ""),
                error=not getattr(result, "ok", False)))

    # -- 保存の検証 ------------------------------------------------------
    def validate(self) -> bool:
        """保存してよいか (設定ダイアログの OK から呼ばれる)。

        窓を特定できないプロファイルは「最初の窓なら何でも」に
        なってしまうので、保存させない。全部空なら未設定に戻す
        操作として受け付ける。理由は状態ラベルに出す。"""
        fields = self.fields()
        if any(fields.values()) and not is_configured(fields):
            self._show_status(
                "窓を特定するために、プロセス名・ウィンドウクラス・"
                "アプリIDの頭のどれかが必要です", error=True)
            return False
        return True


class ToolSettingsPage(QWidget):
    """「ツール設定」タブのページ (2.00、2.01 でタブ化と
    意味の変更)。

    「ツール実行」の合図で呼べるツールの登録で、AI プロファイル
    とは別の、PHAL 全体で共通の設定 (script_runner 節の
    cli_tools / cli_tool_paths / cli_tool_usages)。表の列は
    名前・パス・使い方。実行時は登録されたパスが正で、パスが
    空のまま実行しようとするとエラーになる (2.01)。パスが空の
    行には「探す」ボタンが出て、PATH と Wireshark の置き場を
    探して実パスを記入できる。ツール置き場に入っているものも
    行として出し、パスの欄には「ツール置き場」と表示する
    (置き場の実在が許可になる)。使い方は設定の報告に載って
    AI へ届く (AI が呼び方を知るための文)。実行ファイル
    (.exe / .bat / .cmd / .py) を画面へドラッグ＆ドロップ
    すると、その名前とパスで登録される。名前の整形 (小文字化・
    拡張子の除去) と保存は窓側が core の sanitize で行う。"""

    _DROP_EXTS = (".exe", ".bat", ".cmd", ".py")

    def __init__(self, names, paths, usages, parent=None, *,
                 tools_folder_opener=None, folder_tools=None,
                 tool_searcher=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        # ツールを探す役 (名前 → Path か None)。窓側が core の
        # search_cli_tool を渡す。無ければ「探す」ボタンは
        # 「見つからない」扱いにする。
        self._tool_searcher = tool_searcher
        # ツール置き場に入っているツールの名前 (小文字で持つ)。
        self._folder_names = {
            str(n).strip().lower() for n in (folder_tools or [])}
        layout = QVBoxLayout(self)
        hint = QLabel(
            "「ツール実行」の合図で呼べるツールです"
            "（全プロファイル共通）。実行ファイルをこの画面へ"
            "ドラッグ＆ドロップすると登録できます。「名前を"
            "追加」で名前だけの行も作れます。パスが空の"
            "行は「探す」で探せます (空のまま実行するとエラーに"
            "なります)。使い方は、設定の報告に載って AI へ"
            "届きます。ツール置き場に置いた実行ファイルは、"
            "パスの欄に「ツール置き場」と出て、名前で実行でき"
            "ます。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(
            ["名前", "パス", "使い方（AI向け）"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)
        paths = paths if isinstance(paths, dict) else {}
        usages = usages if isinstance(usages, dict) else {}
        seen = set()
        for name in names or []:
            seen.add(name)
            self._add_row(
                name, paths.get(name, ""), usages.get(name, ""))
        for name, path in paths.items():
            if name not in seen:
                seen.add(name)
                self._add_row(name, path, usages.get(name, ""))
        # ツール置き場の中身も行として出す (登録済みの名前と
        # 重なるものは登録の行が正なので出さない)。
        for name in sorted(self._folder_names):
            if name not in {str(s).lower() for s in seen}:
                self._add_row(name, "", usages.get(name, ""),
                              folder=True)
        self._table.itemChanged.connect(self._on_item_changed)
        row = QHBoxLayout()
        self._add_name_button = QPushButton("名前を追加")
        self._add_name_button.clicked.connect(self._add_name_row)
        row.addWidget(self._add_name_button)
        self._add_button = QPushButton("ファイルを追加…")
        self._add_button.clicked.connect(self._add_files)
        row.addWidget(self._add_button)
        self._remove_button = QPushButton("選んだ行を削除")
        self._remove_button.clicked.connect(self._remove_selected)
        row.addWidget(self._remove_button)
        if tools_folder_opener is not None:
            self._folder_button = QPushButton("ツール置き場を開く")
            self._folder_button.clicked.connect(tools_folder_opener)
            row.addWidget(self._folder_button)
        else:
            self._folder_button = None
        row.addStretch(1)
        layout.addLayout(row)
        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    # -- 行の操作 --------------------------------------------------------
    def _add_row(self, name, path, usage, folder=False):
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(str(name or "")))
        if folder:
            # 置き場の行は、パスの欄に「ツール置き場」と出し、
            # 編集はさせない (実パスは置き場が決める)。
            path_item = QTableWidgetItem("ツール置き場")
            path_item.setFlags(
                path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 1, path_item)
        else:
            self._table.setItem(
                row, 1, QTableWidgetItem(str(path or "")))
        self._table.setItem(row, 2, QTableWidgetItem(str(usage or "")))
        self._sync_search_button(row)

    def _is_folder_row(self, row) -> bool:
        return self._cell(row, 0).lower() in self._folder_names \
            and self._cell(row, 1) == "ツール置き場"

    def _sync_search_button(self, row):
        """パスの欄の「探す」ボタンの付け外し (2.01)。

        置き場の行と、パスが入っている行には出さない。パスが
        空の行にだけ出す。"""
        if row < 0 or row >= self._table.rowCount():
            return
        existing = self._table.cellWidget(row, 1)
        if self._is_folder_row(row) or self._cell(row, 1):
            if existing is not None:
                self._table.setCellWidget(row, 1, None)
            return
        if existing is None:
            button = QPushButton("探す")
            button.clicked.connect(
                lambda _checked=False, btn=button:
                self._on_search_clicked(btn))
            self._table.setCellWidget(row, 1, button)

    def _on_item_changed(self, item):
        if item.column() == 1:
            self._sync_search_button(item.row())

    def _on_search_clicked(self, button):
        """「探す」: PATH と Wireshark の置き場を探し、見つか
        ればその行のパスへ実パスを記入する (2.01)。"""
        row = -1
        for candidate in range(self._table.rowCount()):
            if self._table.cellWidget(candidate, 1) is button:
                row = candidate
                break
        if row < 0:
            return
        name = self._cell(row, 0)
        found = None
        if self._tool_searcher is not None and name:
            try:
                found = self._tool_searcher(name)
            except Exception:  # noqa: BLE001  探せなければ未発見
                found = None
        if found:
            self._table.setItem(
                row, 1, QTableWidgetItem(str(found)))
            self._status_label.setText(
                f"「{name}」が見つかりました: {found}")
        else:
            self._status_label.setText(
                f"「{name}」は PATH と Wireshark の置き場に"
                "見つかりませんでした")

    def _cell(self, row, col) -> str:
        item = self._table.item(row, col)
        return item.text().strip() if item is not None else ""

    def _find_row(self, name) -> int:
        target = str(name or "").strip().lower()
        for row in range(self._table.rowCount()):
            if self._cell(row, 0).lower() == target:
                return row
        return -1

    def _add_tool_file(self, file_path):
        """実行ファイル1件を登録する (D&D とファイル選択の
        共通処理)。名前はファイル名から拡張子を除いたもの。
        同じ名前の行があればパスだけ差し替える。"""
        from pathlib import Path

        path = Path(str(file_path))
        if path.suffix.lower() not in self._DROP_EXTS:
            self._status_label.setText(
                f"実行ファイルではないため登録しませんでした: "
                f"{path.name}")
            return
        name = path.stem
        row = self._find_row(name)
        if row >= 0:
            # 置き場の行だった場合は、登録の行へ作り替える
            # (明示の登録が置き場より優先する)。
            self._folder_names.discard(name.lower())
            self._table.setItem(
                row, 1, QTableWidgetItem(str(path)))
            self._sync_search_button(row)
            self._status_label.setText(
                f"「{name}」のパスを差し替えました")
        else:
            self._add_row(name, str(path), "")
            self._status_label.setText(f"「{name}」を登録しました")

    def _add_name_row(self):
        """名前だけの空の行を足す (2.02)。

        名前を入れてパスが空のままなら「探す」ボタンが出る。
        パスを設定するまで、そのツールは実行時にエラーになる。"""
        self._add_row("", "", "")
        row = self._table.rowCount() - 1
        self._table.setCurrentCell(row, 0)
        item = self._table.item(row, 0)
        if item is not None:
            self._table.editItem(item)

    def _add_files(self):
        files, _selected = QFileDialog.getOpenFileNames(
            self, "ツールの追加", "",
            "実行ファイル (*.exe *.bat *.cmd *.py);;"
            "すべてのファイル (*.*)")
        for file_path in files or []:
            self._add_tool_file(file_path)

    def _remove_selected(self):
        rows = sorted(
            {index.row()
             for index in self._table.selectedIndexes()},
            reverse=True)
        for row in rows:
            self._table.removeRow(row)

    # -- ドラッグ＆ドロップ ----------------------------------------------
    def dragEnterEvent(self, event):  # noqa: N802  Qt の上書き
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802  Qt の上書き
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802  Qt の上書き
        added = False
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self._add_tool_file(url.toLocalFile())
                added = True
        if added:
            event.acceptProposedAction()
        else:
            event.ignore()

    # -- 結果 --------------------------------------------------------------
    def tools(self):
        """表の内容を (名前の並び, パス辞書, 使い方辞書) で
        返す。空の名前の行は除く。ツール置き場の行は、名前と
        パスには入れない (許可は置き場の実在が担う) が、使い方
        は返す。整形は窓側の sanitize が担う。"""
        names = []
        paths = {}
        usages = {}
        for row in range(self._table.rowCount()):
            name = self._cell(row, 0)
            if not name:
                continue
            usage = self._cell(row, 2)
            if usage:
                usages[name] = usage
            if self._is_folder_row(row):
                continue
            names.append(name)
            path = self._cell(row, 1)
            if path:
                paths[name] = path
        return names, paths, usages


class AutomationPage(QWidget):
    """「自動化設定」タブのページ (2.01)。

    script_runner 節の auto_* キー (AI プロファイルとは別の
    共通設定)。既定は全部手動。"""

    def __init__(self, automation=None, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        given = sanitize_automation(automation)
        self._auto_boxes = {}
        for key, label in AUTO_LABELS:
            box = QCheckBox(label)
            box.setChecked(given[key])
            self._auto_boxes[key] = box
            layout.addWidget(box)
        layout.addStretch(1)

    def automation(self) -> dict:
        """自動化の切替の今の値 (script_runner 節へ保存する形)。"""
        return {key: bool(self._auto_boxes[key].isChecked())
                for key in AUTO_KEYS}


class CommonSettingsPage(QWidget):
    """「共通設定」タブのページ (2.01、フォルダ変更は 2.02)。

    プロファイルに依らない設定: 監視フォルダと作業フォルダ
    (「変更…」でフォルダを選べる)、ダークモード、要約モード、
    実行の上限、開始プロンプトの文 (1.77 の編集欄をここへ
    移した)。開始プロンプトは script_runner 節の intro_prompt
    に持つ (投げ先共通の文)。空のときは窓側が環境から
    自動生成する。"""

    def __init__(self, parent=None, *, watch_text="",
                 watch_folder="", work_folder="",
                 dark_mode=False, summary_mode=True,
                 timeout_seconds=120, intro_prompt="",
                 intro_text_provider=None, error_color="#c00000"):
        super().__init__(parent)
        self._error_color = error_color
        self._intro_text_provider = intro_text_provider
        self._watch_folder = watch_folder or ""
        self._work_folder = work_folder or ""
        layout = QVBoxLayout(self)
        form = QFormLayout()
        # フォルダの行は「今の場所の表示＋変更ボタン」。
        # 監視フォルダは、設定が空なら既定 (ダウンロード
        # フォルダ) が実効値として表示される。
        self._watch_label = QLabel(
            self._watch_folder or watch_text or "")
        watch_row = QWidget()
        watch_layout = QHBoxLayout(watch_row)
        watch_layout.setContentsMargins(0, 0, 0, 0)
        watch_layout.addWidget(self._watch_label, 1)
        self._watch_button = QPushButton("変更…")
        self._watch_button.clicked.connect(
            lambda: self._pick_folder("watch"))
        watch_layout.addWidget(self._watch_button)
        form.addRow("監視フォルダ", watch_row)
        self._work_label = QLabel(
            self._work_folder or "(未選択)")
        work_row = QWidget()
        work_layout = QHBoxLayout(work_row)
        work_layout.setContentsMargins(0, 0, 0, 0)
        work_layout.addWidget(self._work_label, 1)
        self._work_button = QPushButton("変更…")
        self._work_button.clicked.connect(
            lambda: self._pick_folder("work"))
        work_layout.addWidget(self._work_button)
        form.addRow("作業フォルダ", work_row)
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(5, 3600)
        try:
            timeout_value = int(timeout_seconds)
        except (TypeError, ValueError):
            timeout_value = 120
        self._timeout_spin.setValue(
            min(max(timeout_value, 5), 3600))
        self._timeout_spin.setSuffix(" 秒")
        form.addRow("実行の上限", self._timeout_spin)
        layout.addLayout(form)
        self._dark_box = QCheckBox("ダークモード")
        self._dark_box.setChecked(bool(dark_mode))
        layout.addWidget(self._dark_box)
        self._summary_box = QCheckBox("要約モード")
        self._summary_box.setChecked(bool(summary_mode))
        layout.addWidget(self._summary_box)

        intro_group = QGroupBox("開始プロンプト")
        intro_layout = QVBoxLayout(intro_group)
        intro_hint = QLabel(
            "「保存した文を使う」にチェックがあるときは、この文を"
            "送ります。無ければ自動生成の文を送ります。")
        intro_hint.setWordWrap(True)
        intro_layout.addWidget(intro_hint)
        # チェックが無ければ編集欄は使わない (1.80)。自動生成の
        # 文を見ただけで保存され、古い文に固定されてしまう事故
        # の対策 (1.77 の実地で発生)。
        self._intro_use_box = QCheckBox(
            "保存した文を使う（自動生成しない）")
        self._intro_use_box.setChecked(bool(intro_prompt))
        self._intro_use_box.toggled.connect(
            self._on_intro_use_toggled)
        intro_layout.addWidget(self._intro_use_box)
        self._intro_edit = QPlainTextEdit()
        self._intro_edit.setPlainText(intro_prompt or "")
        self._intro_edit.setMaximumHeight(150)
        self._intro_edit.setEnabled(bool(intro_prompt))
        intro_layout.addWidget(self._intro_edit)
        intro_row = QHBoxLayout()
        self._intro_load_button = QPushButton("自動生成の文を入れる")
        self._intro_load_button.clicked.connect(
            self._load_intro_text)
        self._intro_load_button.setEnabled(
            intro_text_provider is not None)
        intro_row.addWidget(self._intro_load_button)
        intro_row.addStretch(1)
        intro_layout.addLayout(intro_row)
        layout.addWidget(intro_group)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        layout.addStretch(1)

    def _pick_folder(self, which):
        """フォルダを選んで表示と控えを差し替える (2.02)。

        やめたときは何も変えない。選んだ時点で控えが替わり、
        保存はダイアログの OK でまとめて行われる。"""
        if which == "watch":
            start = self._watch_folder or self._watch_label.text()
        else:
            start = self._work_folder
        chosen = QFileDialog.getExistingDirectory(
            self,
            "監視フォルダの選択" if which == "watch"
            else "作業フォルダの選択",
            start or "")
        if not chosen:
            return
        if which == "watch":
            self._watch_folder = chosen
            self._watch_label.setText(chosen)
        else:
            self._work_folder = chosen
            self._work_label.setText(chosen)

    def values(self) -> dict:
        """共通設定の今の値 (script_runner 節へ保存する形)。"""
        return {
            "dark_mode": bool(self._dark_box.isChecked()),
            "summary_mode": bool(self._summary_box.isChecked()),
            "timeout_seconds": int(self._timeout_spin.value()),
            "watch_folder": self._watch_folder,
            "work_folder": self._work_folder,
        }

    def intro_prompt(self) -> str:
        """開始プロンプトの保存内容。「保存した文を使う」に
        チェックが無ければ空 (自動生成) を返す。"""
        if not self._intro_use_box.isChecked():
            return ""
        return self._intro_edit.toPlainText()

    def _on_intro_use_toggled(self, on):
        self._intro_edit.setEnabled(bool(on))

    def _load_intro_text(self):
        """自動生成の文を編集欄へ入れる (編集の土台にする)。

        入れたら「保存した文を使う」にもチェックを入れる
        (土台として取り込んだ意思の明示)。"""
        if self._intro_text_provider is None:
            return
        try:
            text = self._intro_text_provider()
        except Exception as e:  # noqa: BLE001  状態行で知らせる
            self._show_status(
                f"自動生成の文を作れませんでした（{e}）", error=True)
            return
        self._intro_edit.setPlainText(text or "")
        self._intro_use_box.setChecked(True)
        self._show_status("自動生成の文を入れました")

    def _show_status(self, message, error=False):
        self._status_label.setStyleSheet(
            f"color: {self._error_color};" if error else "")
        self._status_label.setText(message)


class SettingsDialog(QDialog):
    """PHAL の設定ウィンドウ (2.01 でタブ化)。

    タブは 共通設定 / AI別設定 / 動作ログ / 自動化設定 /
    ツール設定。入口のボタンによって初期タブだけが変わり、
    OK で全タブ分をまとめて保存する (窓側が受け取る)。
    AI別設定の検証が通らないときは、そのタブへ移して止める。"""

    _TAB_DEFS = (
        ("common", "共通設定"),
        ("ai", "AI別設定"),
        ("log", "動作ログ"),
        ("auto", "自動化設定"),
        ("tools", "ツール設定"),
    )

    def __init__(self, fields, parent=None, *, profiles=None,
                 active_index=0, automation=None, common=None,
                 tools_data=None, enumerate_windows=None,
                 scanner=None, filler=None, button_scanner=None,
                 attach_tester=None, diag_text_provider=None,
                 error_color="#c00000", intro_text_provider=None,
                 tools_folder_opener=None, tool_searcher=None,
                 initial_tab="ai", watch_names=None):
        super().__init__(parent)
        self.setWindowTitle("設定 - PHAL")
        self.resize(720, 800)
        common = common or {}
        tools_data = tools_data or {}
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self.common_page = CommonSettingsPage(
            self,
            watch_text=common.get("watch_text", ""),
            watch_folder=common.get("watch_folder", ""),
            work_folder=common.get("work_folder", ""),
            dark_mode=common.get("dark_mode", False),
            summary_mode=common.get("summary_mode", True),
            timeout_seconds=common.get("timeout_seconds", 120),
            intro_prompt=common.get("intro_prompt", ""),
            intro_text_provider=intro_text_provider,
            error_color=error_color)
        self.ai_page = AiProfilePage(
            fields, self,
            enumerate_windows=enumerate_windows, scanner=scanner,
            filler=filler, button_scanner=button_scanner,
            attach_tester=attach_tester, profiles=profiles,
            active_index=active_index,
            diag_text_provider=diag_text_provider,
            error_color=error_color,
            watch_names=watch_names)
        self.log_page = LogViewerPage(self)
        self.automation_page = AutomationPage(automation, self)
        self.tools_page = ToolSettingsPage(
            tools_data.get("names") or [],
            tools_data.get("paths") or {},
            tools_data.get("usages") or {},
            self, tools_folder_opener=tools_folder_opener,
            folder_tools=tools_data.get("folder_tools") or [],
            tool_searcher=tool_searcher)
        pages = {
            "common": self.common_page,
            "ai": self.ai_page,
            "log": self.log_page,
            "auto": self.automation_page,
            "tools": self.tools_page,
        }
        self._tab_keys = []
        for key, label in self._TAB_DEFS:
            self._tabs.addTab(pages[key], label)
            self._tab_keys.append(key)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self._tabs, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if initial_tab in self._tab_keys:
            self._tabs.setCurrentIndex(
                self._tab_keys.index(initial_tab))

    def _on_tab_changed(self, index):
        if 0 <= index < len(self._tab_keys) \
                and self._tab_keys[index] == "log":
            self.log_page.refresh()

    def accept(self):  # noqa: N802  Qt の上書き
        if not self.ai_page.validate():
            self._tabs.setCurrentIndex(self._tab_keys.index("ai"))
            return
        super().accept()

    # -- 各ページへの委譲 --------------------------------------------
    def profiles(self) -> list:
        return self.ai_page.profiles()

    def active_index(self) -> int:
        return self.ai_page.active_index()

    def watch_names(self) -> list:
        return self.ai_page.watch_names()

    def automation(self) -> dict:
        return self.automation_page.automation()

    def intro_prompt(self) -> str:
        return self.common_page.intro_prompt()

    def common_values(self) -> dict:
        return self.common_page.values()

    def tools(self):
        return self.tools_page.tools()
