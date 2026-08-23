# snippets.py
# 定型文(スニペット)をクリップボードへコピーする部品。トレイアイコンは持たない部品で、
# 開閉の管理は feature_screen 側が行う(color_picker.py と同じ形)。
#
# テンプレートは snippets/ フォルダに置いた *.txt / *.md で、ファイル名(拡張子を除く)が
# そのまま表示名になる。専用の編集UIは持たず、テキストエディタで足せることを狙っている。
#
# 選択ウインドウ自体は picker.PickerWindow に切り出してある(フォルダブックマークと共用)。
# ここに残っているのは「テンプレートの読み込み」「変数の展開」「テンプレートファイルの
# 新規作成・編集を外部エディタに投げる部分」だけ。
import json
import locale
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QInputDialog

import settings as settings_module
from picker import PickerWindow
from toast import show_toast

SNIPPETS_DIR = Path(__file__).resolve().parent / "snippets"
TEMPLATE_SUFFIXES = (".txt", ".md")

# {名前} と {名前:引数} の両方を拾う。既知の名前だけを正規表現に並べてあるので、
# テンプレートにコード片が混じっていて {foo} があってもそのまま残る。str.format() は
# 使わない(未知の変数でKeyErrorになり、テンプレート全体が壊れるため)。
VARIABLE_RE = re.compile(r"\{(clipboard|date|time|datetime|input)(?::([^}]*))?\}")

# 書式を省略したときの既定
DEFAULT_FORMATS = {
    "date": "%Y/%m/%d",
    "time": "%H:%M",
    "datetime": "%Y/%m/%d %H:%M",
}

# Windowsのファイル名に使えない文字。新規作成で弾くために持つ。
INVALID_NAME_CHARS = '\\/:*?"<>|'

# 最近使ったテンプレート名を settings.json に貯める上限。多すぎても並び順への効きが
# 薄れるだけなので、一覧の見える範囲と同程度に切る。
RECENT_LIMIT = 20

PLACEHOLDER = "絞り込み（↑↓選択 / Enter コピー / Ctrl+N 新規 / Ctrl+E 編集 / Esc 閉じる）"

# ウインドウ下部に出す早見表。キー操作はプレースホルダにも書いてあるが、変数の書式は
# 覚えていられないので常に見えるところに置く。
HINT = (
    "Ctrl+N 新規　Ctrl+E 編集　Ctrl+O フォルダを開く\n"
    "{clipboard} クリップボード　{date} 日付　{time} 時刻　{datetime} 日時\n"
    "{date:%Y-%m-%d} 書式指定　{input:ラベル} 実行時に入力"
)

# ロケール設定は1回で足りる。プロセス全体に効く操作なので、日時を実際に使うまで遅らせる。
_locale_ready = False


def _ensure_time_locale() -> None:
    """%a(曜日)などを日本語で出すため、環境のロケールに合わせる。
    失敗しても日時展開自体は続けられる(英語表記になるだけ)ので握りつぶす。"""
    global _locale_ready
    if _locale_ready:
        return
    _locale_ready = True
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        pass


def load_templates(recent: list = None) -> list:
    """snippets/ 配下のテンプレートを [(表示名, 本文), ...] で返す。並びはファイル名順。

    recent(最近使った表示名のリスト)を渡すと、その順で前に寄せ、載っていないものは
    ファイル名順のまま後ろに続ける。「よく使うものが上にある」状態を作るのが目的で、
    並べ替えは安定ソートなので後ろ側の並びは崩れない。

    フォルダが無ければ作る(初回起動でも「入れる場所」が見えるようにするため)。
    BOM付きUTF-8で保存されることがあるので utf-8-sig で読む。"""
    try:
        SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[tray-tools] snippets フォルダを作成できません: {e}", file=sys.stderr)
        return []

    templates = []
    for path in sorted(SNIPPETS_DIR.iterdir(), key=lambda p: p.name):
        if not path.is_file() or path.suffix.lower() not in TEMPLATE_SUFFIXES:
            continue
        try:
            body = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            # 黙って飛ばすと「テンプレートが出てこない」理由が分からなくなる
            print(f"[tray-tools] 定型文を読めません ({path.name}): {e}", file=sys.stderr)
            continue
        templates.append((path.stem, body))

    if recent:
        order = {name: index for index, name in enumerate(recent)}
        templates.sort(key=lambda entry: order.get(entry[0], len(order)))
    return templates


def template_path(name: str):
    """表示名から実ファイルを探す。見つからなければ None。

    表示名は拡張子を落としたファイル名なので、対応拡張子を順に当ててみるしかない。
    ピッカーには本文しか渡していないため、編集で開くときはここで引き直す。"""
    for suffix in TEMPLATE_SUFFIXES:
        path = SNIPPETS_DIR / f"{name}{suffix}"
        if path.is_file():
            return path
    return None


def _open_path(path) -> bool:
    """関連付けアプリ(フォルダならエクスプローラ)で開く。成否を返す。

    os.startfile は関連付けが無い・パスが消えたなどで例外を投げる。Qtのスロット内で
    投げ切ると常駐アプリごと落ちるので、必ずここで受けて通知に回す。"""
    try:
        os.startfile(str(path))
        return True
    except OSError as e:
        show_toast(f"定型文\n開けませんでした\n{path}\n{e}")
        return False


def open_folder() -> None:
    """snippets/ フォルダを開く。テンプレートが1件も無い状態からでも辿り着けるように、
    無ければ作ってから開く(load_templates と同じ理由)。"""
    try:
        SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        show_toast(f"定型文\nフォルダを作成できません\n{e}")
        return
    _open_path(SNIPPETS_DIR)


def edit_template(name: str) -> None:
    """既存のテンプレートを関連付けアプリで開く。"""
    path = template_path(name)
    if path is None:
        show_toast(f"定型文\nファイルが見つかりません\n{name}")
        return
    _open_path(path)


def create_template(parent=None) -> None:
    """名前を尋ねて snippets/<名前>.txt を空で作り、関連付けアプリで開く。

    同名があれば作らずに開くだけにする。上書きすると、書き溜めたテンプレートを
    名前の打ち間違いひとつで消してしまうため。"""
    name, ok = QInputDialog.getText(
        parent, "定型文の新規作成", "テンプレート名（そのままファイル名になります）"
    )
    name = name.strip() if ok else None
    if not name:
        return

    if any(char in name for char in INVALID_NAME_CHARS):
        show_toast(f"定型文\nファイル名に使えない文字が入っています\n{INVALID_NAME_CHARS}")
        return

    existing = template_path(name)
    if existing is not None:
        show_toast(f"定型文\n同じ名前のテンプレートがあります\n{existing.name}")
        _open_path(existing)
        return

    path = SNIPPETS_DIR / f"{name}{TEMPLATE_SUFFIXES[0]}"
    try:
        SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    except OSError as e:
        show_toast(f"定型文\n作成できませんでした\n{name}\n{e}")
        return
    _open_path(path)


def load_recent(app_settings: dict) -> list:
    """settings.json の snippets.recent(最近コピーした表示名)を返す。

    手で編集されうるファイルなので、文字列でない要素は黙って捨てる(壊れた1件で
    並び替え全体を諦めるほどのものではない)。"""
    section = app_settings.get("snippets")
    recent = section.get("recent", []) if isinstance(section, dict) else []
    if not isinstance(recent, list):
        return []
    return [name for name in recent if isinstance(name, str) and name]


def push_recent(app_settings: dict, settings_path, name: str) -> bool:
    """使ったテンプレート名を recent の先頭へ移して settings.json に保存する。成否を返す。

    メモリ上の app_settings はデフォルト値をマージ済みなので、それを丸ごと書き出すと
    未設定の既定値まで明示的に書かれてファイルの姿が変わってしまう。launcher.save_bookmark
    と同じく、保存はファイルを読み直して snippets.recent だけを差し替える形にする。
    メモリ上の app_settings も更新しておく(アプリを再起動するまで settings.json を
    読み直さないため、次に開いたときの並びに効かせたい)。"""
    recent = [existing for existing in load_recent(app_settings) if existing != name]
    recent.insert(0, name)
    del recent[RECENT_LIMIT:]

    section = app_settings.get("snippets")
    if not isinstance(section, dict):
        section = app_settings["snippets"] = {}
    section["recent"] = recent

    if not settings_path:
        return False
    try:
        stored = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
        stored_snippets = stored.get("snippets")
        if not isinstance(stored_snippets, dict):
            stored_snippets = stored["snippets"] = {}
        stored_snippets["recent"] = recent
        settings_module.save_settings(stored, settings_path)
        return True
    except (OSError, ValueError, TypeError, AttributeError) as e:
        print(f"[tray-tools] 定型文の使用履歴を保存できません: {e}", file=sys.stderr)
        return False


def _format_now(kind: str, spec: str, original: str) -> str:
    _ensure_time_locale()
    fmt = spec or DEFAULT_FORMATS[kind]
    try:
        return datetime.now().strftime(fmt)
    except ValueError as e:
        # Windowsでは未知の書式指定子で例外になる。テンプレート全体を捨てるほどでは
        # ないので、その変数だけ元の記法({date:...})のまま残す。
        print(f"[tray-tools] 日時書式が不正です ({fmt}): {e}", file=sys.stderr)
        return original


def _ask_input(parent, label: str) -> str:
    """{input} の入力を求める。キャンセルされたら None を返す(展開全体の中止を意味する)。

    ラベルはタイトルバーにも出す。同じテンプレートで複数の {input} を続けて聞かれると、
    本文中のラベルだけでは「今どれを聞かれているのか」が分かりにくいため。"""
    label = label or "入力"
    text, ok = QInputDialog.getMultiLineText(parent, f"定型文 - {label}", label)
    return text if ok else None


def expand_variables(text: str, parent=None, preview: bool = False):
    """テンプレート中の変数を展開した文字列を返す。{input} がキャンセルされたら None。

    同じ {input:ラベル} が複数回出てきても尋ねるのは1回だけで、以降は同じ値を使う
    (同じ内容を2回打たされるのを避けるため)。未知の変数は正規表現に載せていないので
    そのまま残る。

    preview=True は選択ウインドウのプレビュー用。{input:ラベル} を尋ねずに 【ラベル】 へ
    置き換える(プレビューは選択が動くたびに呼ばれるので、入力ダイアログを出したら
    一覧を眺めることすらできない)。他の変数は通常どおり展開するので「何がコピーされるか」は
    そのまま見える。尋ねない以上キャンセルも起きないため、この場合 None は返らない。"""
    answers = {}
    result = []
    position = 0

    for match in VARIABLE_RE.finditer(text):
        result.append(text[position:match.start()])
        position = match.end()
        name = match.group(1)
        spec = match.group(2)

        if name == "clipboard":
            result.append(QGuiApplication.clipboard().text())
        elif name == "input":
            label = spec or "入力"
            if preview:
                result.append(f"【{label}】")
                continue
            if label not in answers:
                answer = _ask_input(parent, label)
                if answer is None:
                    return None
                answers[label] = answer
            result.append(answers[label])
        else:
            result.append(_format_now(name, spec, match.group(0)))

    result.append(text[position:])
    return "".join(result)


def create_picker(app_settings: dict, settings_path=None):
    """選択ウインドウを作って返す。テンプレートが1件も無ければ通知だけ出して None を返す
    (空のウインドウを出しても操作できることが無く、置き場所も伝わらないため。この場合は
    トレイメニューの「定型文フォルダを開く」から辿る)。

    並びは settings.json の snippets.recent(最近使った順)。コピーしたら履歴を更新するので、
    保存先として app_settings と settings_path を受け取る。"""
    templates = load_templates(load_recent(app_settings))
    if not templates:
        show_toast(f"定型文\nsnippets フォルダにテンプレートがありません\n{SNIPPETS_DIR}")
        return None

    # picker を先に None で置いてから代入するのは、_accept が呼ばれる時点では必ず
    # 代入済みになるため(入力ダイアログの親にウインドウ自身を渡したいが、生成時には
    # まだそのオブジェクトが無い)。
    picker = None

    def _accept(name: str, body: str) -> None:
        expanded = expand_variables(body, picker)
        if expanded is None:
            # 入力ダイアログでキャンセルされた。中途半端な文字列は載せずに引き下がる。
            return
        QGuiApplication.clipboard().setText(expanded)
        push_recent(app_settings, settings_path, name)
        show_toast(f"定型文をコピーしました\n{name}")

    def _preview(body: str) -> str:
        # 「何がコピーされるか」を見せるのが狙いなので、変数を展開した後の姿を返す。
        # {input} だけは preview=True で尋ねずに 【ラベル】 のまま置く。
        return expand_variables(body, preview=True)

    picker = PickerWindow(
        "定型文",
        templates,
        _accept,
        placeholder=PLACEHOLDER,
        preview_provider=_preview,
        hint=HINT,
        on_new=lambda: create_template(picker),
        on_edit=lambda name, _body: edit_template(name),
        on_open_folder=open_folder,
    )
    return picker
