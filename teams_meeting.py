# teams_meeting.py
# Teams の「一人会議」を、ホットキー1つで立ち上げる部品。トレイアイコンは持たない。
# 開閉(入口・通知)の管理は feature_screen 側が持つ(snippets.py と同じ形)。
#
# 【何のためにあるか】
# 一人会議で話したトランスクリプトを Copilot に渡して資料を作っている。続けて同じ会議で
# 話すとトランスクリプトの生成が遅れたり、Copilot が一つ前の会議をまとめたりするので、
# 毎回新しい会議を立てている。その「今すぐ会議 → 会議名 → 開始 → 参加 → 文字起こし」を
# 1回で済ませる。会議名に日時(とテーマ)を入れてユニークにしておけば、Copilot へ
# 「『一人会議 10/08 14:30 ○○』の内容をまとめて」と名前で指示できる。
#
# 【UIA だけを使う。キー送信・マウス・SetForegroundWindow は一切しない】
# 前面化の切り替えで別の窓に誤入力する事故が実際に起きている(陽太さんの安全ルール)。
# UIA なら要素を名指しで押せるので、別の窓へ飛ぶ余地が原理的に無い。
# ただし Teams の「今すぐ会議」のダイアログは、開くと自分でフォーカスを取る。実測で、
# 別の窓で打っていた「susum」が会議名の末尾に混ざった。だから
#   - 会議名を入れたら間を置かずに「会議を開始」を押す(delay_after を 0 にしてある)
#   - 入れた直後に読み戻して、混ざっていたら入れ直す
#   - 開始時に「数秒間キー入力を控えてください」と知らせる(feature_screen 側)
#
# 【常駐の中では UIA を使わない(別プロセスで回す)】
# 常駐は音声切替(pycaw)を持っていて、UIA と pycaw を同じプロセスに置くと GC の
# たびに 0xC0000005 で即死する(実測値は copilot_watchdog.py 冒頭)。**スレッドを
# 分けても同じプロセスなら助からない**(agent_loop.spawn の docstring)。だから
# 実処理はこのファイルを子プロセスとして起こして回し、進み具合は標準出力に1行1件の
# JSON で返す。常駐の側は読み役のスレッドでそれを受け、Qt のシグナルでメインスレッドへ
# 渡す(agent_loop.spawn と _AgentLoopBridge の流儀)。
# 常駐が import してよいのは、このファイルの「COM に触らない部分」だけ。comtypes の
# import は UiaBackend の中に閉じ込めてある。psutil も使わない(内部で COM を使い、
# 常駐が即死した実例がある。copilot_watchdog.py 冒頭)。
#
# 【手順はデータで持つ】
# 「どの窓で・どの要素を・何をする・待ち時間・任意かどうか」を BUILTIN_STEPS に並べ、
# Engine がそれを順に回す。業務PCへはコードを配りにくいので、settings.json の
# teams_meeting.steps で手順のキー単位に上書き・追加できる(copilot_profiles と同じ
# 考え方)。業務版の文字起こしの場所は未確認で、候補名をリストで持たせてある。
#
# 【UIA とエンジンを分けてある】
# Engine は「窓を並べる・要素を探す・押す・開く・値を入れる・名前を読む」という薄い
# 口(UiaBackend)しか知らない。テスト(tests/test_teams_meeting.py)はここに偽の
# 要素ツリーを差し込み、実物の Teams に触らずに手順の流れを確かめる。
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent

SETTINGS_KEY = "teams_meeting"

DEFAULT_TITLE_FORMAT = "一人会議 {date:%m/%d %H:%M} {topic}"

# 要素を探し直す間隔(秒)。Teams の画面遷移は数百ms〜数秒なので、これより細かくしても
# 早くはならず、木の走査(プロセス間の COM 呼び出し)が増えるだけ。
DEFAULT_POLL_SECONDS = 0.3

# 窓の探し方と起動のしかた。
#
# 【なぜ exe 名だけで照合するのか】
# このPC(無料版・新 Teams)の実測は exe=ms-teams.exe / class=TeamsWebView。
# タイトルは開いている画面で変わる(「チャット | … | Microsoft Teams」)。クラスは
# 業務版で同じかを確かめていないので、既定では条件にしない(window_class を書けば
# 絞れる)。会議の窓も同じ exe なので、exe で拾ってから手順ごとに窓を選ぶ。
#
# 【起動は候補を順に試す】
# ms-teams: プロトコルで起きない環境のために、ストアアプリとしての起動も並べてある。
BUILTIN_APP = {
    "process_name": "ms-teams.exe",
    "window_class": "",
    "launch": [
        "ms-teams:",
        r"shell:AppsFolder\MSTeams_8wekyb3d8bbwe!MSTeams",
    ],
}

# 既定の手順。上から順に回す。各項目の意味:
#   key        手順の名前。settings.json での上書き・追加はこれで指す
#   label      通知とログに出す名前
#   action     ensure_app(窓が無ければ起動して待つ) / wait(現れるのを待つだけ) /
#              invoke(押す) / expand(開く) / set_value(値を入れる) /
#              ensure_state(ボタン名から状態を読み、望みと違えば押す) /
#              path(メニューを順にたどる。routes で別経路を並べられる)
#   window     どの窓で探すか。any(Teams の全部の窓) / main(remember_window で
#              覚えた窓) / meeting(会議の窓。下の _windows を参照)
#   find       探す要素。automation_id / name(完全一致) / name_prefix(前方一致) /
#              control_type / within(この要素の中だけを探す) を、どれも文字列か
#              リストで書ける。リストは先頭ほど優先で、どれかに当たれば見つかったとする
#   timeout    見つかるまで待つ秒数。過ぎたら止まる(optional なら飛ばして進む)
#   optional   true なら、見つからなくても止めずに先へ進む
#   delay_after 終わってから次の手順へ移るまでの待ち(秒)
#
# 【ボタン名をリストで持つ理由】copilot_loop と同じ。文言はアプリ・版・表示言語で
# 変わる。1つの完全一致に賭けると、少し違うだけで止まり、しかも理由が画面に出ない。
BUILTIN_STEPS = [
    {
        "key": "teams",
        "label": "Teams の窓を探す",
        "action": "ensure_app",
        # 起動から窓が出るまで。新 Teams は冷えた状態からだと十数秒かかる。
        "timeout": 30,
    },
    {
        # Teams が「チャット」以外の画面(設定・予定表など)を開いていると、今すぐ会議の
        # ボタンが居ない。そのときだけ左端の「チャット」を押して画面を戻す。
        # 押すのは画面の切り替えだけで、何かを作ったり送ったりはしない。
        #
        # aid は Teams のチャットアプリの ID(実測)。テナントに依らない固定値の見込み
        # だが、業務版では未確認なので名前でも探す。control_type を Button に絞るのは、
        # 設定画面の「チャット」タブ(TabItem)を押さないため(実測で同名が居た)。
        "key": "open_chat",
        "label": "チャット画面へ切り替える",
        "action": "invoke",
        "window": "any",
        "skip_if_found": {"name": ["今すぐ会議", "Meet now"], "control_type": "Button"},
        "find": {
            "automation_id": ["86fcd49b-61a2-4701-b771-54728cd291fb"],
            "name": ["チャット", "Chat"],
            "control_type": "Button",
        },
        "timeout": 5,
        "optional": True,
        "delay_after": 0.5,
    },
    {
        # 画面上は文字の無いビデオカメラのアイコン。AutomationId は付いていない。
        "key": "meet_now",
        "label": "今すぐ会議",
        "action": "invoke",
        "window": "any",
        # ここで見つけた窓を以降「main」と呼ぶ。ダイアログはこの窓の中に出る。
        # 前の会議の窓が残っていても、そちらの「閉じる」などを押さないため。
        "remember_window": "main",
        "find": {"name": ["今すぐ会議", "Meet now"], "control_type": "Button"},
        # 起動直後は画面が出そろうまでかかるので長めに待つ。
        "timeout": 30,
    },
    {
        "key": "title",
        "label": "会議名を入れる",
        "action": "set_value",
        "window": "main",
        # ValuePattern.SetValue で入れると内部の状態にも反映される(実測)。
        "find": {"automation_id": ["meeting-title-input"]},
        "value": "{title}",
        "timeout": 10,
        # フォーカスがこの欄に残る時間を最小にする。待たずに次(会議を開始)へ。
        "delay_after": 0,
    },
    {
        "key": "start",
        "label": "会議を開始",
        "action": "invoke",
        "window": "main",
        "find": {"name": ["会議を開始", "Start meeting"], "control_type": "Button"},
        # 押す直前の窓を覚えておき、押したあとに増えた窓を「会議の窓」とみなす。
        "snapshot_windows": True,
        "timeout": 5,
    },
    {
        # 会議への参加(参加前の画面)。新しい窓で開く。タイトルは
        # 「会議への参加 | <会議名> | Microsoft Teams」。
        "key": "prejoin",
        "label": "参加前の画面を待つ",
        "action": "wait",
        "window": "meeting",
        "find": {"automation_id": ["prejoin-join-button"],
                 "name": ["今すぐ参加", "Join now"]},
        "timeout": 30,
    },
    {
        # ボタン名は「押すと何が起きるか」を表している。
        #   「カメラをオンにします(Ctrl+Shift+O)」= いまオフ(押すとオン)
        # だから turn_on_names に当たれば「いまオフ」、turn_off_names なら「いまオン」。
        # 両方に前方一致しうるときは長く一致したほうを採る(_classify_state)。
        "key": "camera",
        "label": "カメラ",
        "action": "ensure_state",
        "window": "meeting",
        "want": "{camera}",
        "find": {
            "name_prefix": ["カメラをオン", "カメラをオフ", "Turn camera on",
                            "Turn camera off", "Turn on camera", "Turn off camera"],
            "control_type": ["Button", "CheckBox"],
        },
        "turn_on_names": ["カメラをオンにします", "カメラをオンにする", "カメラをオン",
                          "Turn camera on", "Turn on camera"],
        "turn_off_names": ["カメラをオフにします", "カメラをオフにする", "カメラをオフ",
                           "Turn camera off", "Turn off camera"],
        # 参加前の画面が出たあとなので、居るならすぐ見つかる。
        "timeout": 3,
        "optional": True,
    },
    {
        #   「マイクをミュート(Ctrl+Shift+M)」= いまオン(押すとミュート)
        # 「マイクのミュートを解除」は「マイクのミュート」と前方一致が重なるので、
        # 長く一致したほうを採る作りに頼っている。
        "key": "mic",
        "label": "マイク",
        "action": "ensure_state",
        "window": "meeting",
        "want": "{mic}",
        "find": {
            "name_prefix": ["マイクのミュートを解除", "マイクのミュート解除", "ミュートを解除",
                            "マイクをミュート", "Unmute", "Mute"],
            "control_type": ["Button", "CheckBox"],
        },
        "turn_on_names": ["マイクのミュートを解除", "マイクのミュート解除", "ミュートを解除",
                          "Unmute mic", "Unmute"],
        "turn_off_names": ["マイクをミュート", "Mute mic", "Mute"],
        "timeout": 3,
        "optional": True,
    },
    {
        "key": "join",
        "label": "今すぐ参加",
        "action": "invoke",
        "window": "meeting",
        "find": {"automation_id": ["prejoin-join-button"],
                 "name": ["今すぐ参加", "Join now"]},
        "timeout": 10,
    },
    {
        # 参加できたかを「退出」ボタンが出たかで確かめる。これが出ないうちに次へ
        # 進むと、招待ダイアログも文字起こしも「見つからない」で飛ばされてしまう。
        "key": "joined",
        "label": "参加を確かめる",
        "action": "wait",
        "window": "meeting",
        "find": {"automation_id": ["hangup-button"],
                 "name": ["退出します", "退出", "Leave"]},
        "timeout": 30,
    },
    {
        # 参加直後に自動で出る招待のダイアログ。
        #
        # 【必ず within で中に絞ること】
        # 「閉じる」は窓のタイトルバーにも居る(実測: Button '閉じる'、aid なし)。窓全体
        # から探すと、会議の窓そのものを閉じかねない。ダイアログが見つからなければ
        # 「閉じる」も探さない(_find_in の within を参照)。
        "key": "close_invite",
        "label": "招待のダイアログを閉じる",
        "action": "invoke",
        "window": "meeting",
        "find": {
            "within": {
                "name": ["会議への参加を求めるユーザーを招待してください",
                         "Invite people to join you"],
                "control_type": ["Window", "Pane", "Group", "Custom"],
            },
            "name": ["閉じる", "Close"],
            "control_type": "Button",
        },
        "timeout": 8,
        "optional": True,
        "delay_after": 0.5,
    },
    {
        # 業務版の文字起こし。無料版には無い(実測。録画ボタンは設定かアップグレードの
        # 画面を開くだけだった)ので、ここは任意。
        #
        # 経路は「その他 → 録画と文字起こし → 文字起こしの開始」。
        # 【上部の「録画」ボタンを既定の経路に入れない理由】
        # 版によっては押した瞬間に録画が始まる(メニューが開くとは限らない)。望まない
        # 録画は取り消しが利かないので、メニューを開くだけで済む「その他」からたどる。
        # 録画ボタンのメニューに出る版だと分かったら、settings.json で routes に足す。
        #
        # 「その他」は InvokePattern を持たず、ExpandCollapsePattern で開く(実測)。
        "key": "transcript",
        "label": "文字起こしを開始",
        "action": "path",
        "window": "meeting",
        "routes": [
            [
                {"find": {"automation_id": ["callingButtons-showMoreBtn"],
                          "name": ["その他", "More"]},
                 "action": "expand"},
                {"find": {"name": ["録画と文字起こし", "Record and transcribe"]},
                 "action": "expand"},
                {"find": {"name": ["文字起こしの開始", "文字起こしを開始",
                                   "トランスクリプトを開始", "Start transcription"]},
                 "action": "invoke"},
            ],
        ],
        "timeout": 8,
        "optional": True,
    },
]

# path の2段目以降を待つ秒数の既定。1段目は手順の timeout を使う(画面に出るまでの
# 待ちを含むため)。メニューは開けばすぐ出るので短くてよい。
PATH_SUB_TIMEOUT = 4

# ensure_state で押したあと、ボタン名が切り替わるのを待つ秒数。
STATE_VERIFY_SECONDS = 3

# set_value の入れ直しの回数。読み戻して違っていたら入れ直す。
SET_VALUE_ATTEMPTS = 3

# 状態の値。設定に書く値でもある。
STATE_ON, STATE_OFF, STATE_KEEP = "on", "off", "keep"

TeamsWindow = namedtuple("TeamsWindow", "hwnd title")


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
def _section(app_settings) -> dict:
    if isinstance(app_settings, dict):
        found = app_settings.get(SETTINGS_KEY)
        if isinstance(found, dict):
            return found
    return {}


def merge_steps(builtin, overrides) -> list:
    """既定の手順に settings.json の上書き・追加を重ねる。

    overrides はリスト([{key, ...}, ...])でも辞書({key: {...}})でも書ける。
      - 既定にある key … 書いた項目だけ差し替える(浅いマージ)。find を書けば find が
        丸ごと入れ替わる。timeout だけ伸ばす、のような直し方ができるようにするため
      - 既定に無い key … 新しい手順として足す。"after": "<key>" / "before": "<key>"
        で位置を決められる。どちらも無ければ末尾
      - "disabled": true … その手順を飛ばす
    壊れた項目(key の無いもの・辞書でないもの)は黙って捨てる。設定の書き損じで
    立ち上げ全体を止めたくないため。"""
    steps = [dict(s) for s in builtin]
    if isinstance(overrides, dict):
        overrides = [dict(v, key=k) for k, v in overrides.items() if isinstance(v, dict)]
    if not isinstance(overrides, list):
        return steps
    for item in overrides:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        key = item["key"]
        index = next((i for i, s in enumerate(steps) if s.get("key") == key), None)
        if index is not None:
            merged = dict(steps[index])
            merged.update({k: v for k, v in item.items() if k not in ("after", "before")})
            steps[index] = merged
            continue
        new = {k: v for k, v in item.items() if k not in ("after", "before")}
        anchor = item.get("after") or item.get("before")
        pos = next((i for i, s in enumerate(steps) if s.get("key") == anchor), None)
        if pos is None:
            steps.append(new)
        elif item.get("after"):
            steps.insert(pos + 1, new)
        else:
            steps.insert(pos, new)
    return steps


def load_config(app_settings=None) -> dict:
    """settings.json の teams_meeting を読んで、実行に要るものをまとめて返す。

    窓の探し方(app)も steps と同じく浅いマージで上書きできる。"""
    section = _section(app_settings)
    app = dict(BUILTIN_APP)
    if isinstance(section.get("app"), dict):
        app.update(section["app"])
    camera = str(section.get("camera", STATE_OFF) or STATE_KEEP).lower()
    mic = str(section.get("mic", STATE_ON) or STATE_KEEP).lower()
    try:
        poll = float(section.get("poll_seconds", DEFAULT_POLL_SECONDS))
    except (TypeError, ValueError):
        poll = DEFAULT_POLL_SECONDS
    if not 0.05 <= poll <= 5:
        poll = DEFAULT_POLL_SECONDS
    return {
        "app": app,
        "steps": merge_steps(BUILTIN_STEPS, section.get("steps")),
        "title_format": section.get("title_format") or DEFAULT_TITLE_FORMAT,
        "camera": camera if camera in (STATE_ON, STATE_OFF, STATE_KEEP) else STATE_OFF,
        "mic": mic if mic in (STATE_ON, STATE_OFF, STATE_KEEP) else STATE_ON,
        "poll_seconds": poll,
    }


# ---------------------------------------------------------------------------
# 会議名
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"\{(date|time|datetime)(?::([^}]*))?\}")
_DEFAULT_DATE_FORMATS = {"date": "%Y/%m/%d", "time": "%H:%M", "datetime": "%Y/%m/%d %H:%M"}


def _date_tools():
    """日時の書き方は定型文(snippets)と揃える。同じ {date:書式} で同じ結果になるように。

    snippets は PySide6 を読み込むので、使うときまで import しない(子プロセスは
    会議名を作らないので、そちらで Qt を読む理由が無い)。読めなければ同じ規則の
    控えを使う。"""
    try:
        import snippets
        snippets._ensure_time_locale()
        return snippets.DEFAULT_FORMATS
    except Exception:  # noqa: BLE001  Qt が無い環境でも会議名は作れるように
        return _DEFAULT_DATE_FORMATS


def format_title(fmt=None, topic="", now=None) -> str:
    """会議名を作る。{date:書式} / {time} / {datetime} / {topic} を展開する。

    topic は1行に畳む(会議名に改行は入れられない)。空なら {topic} は消え、残った
    空白の連なりは1つに詰め、両端の空白は落とす。「一人会議 10/08 14:30 」のように
    末尾に空白が残ると、Copilot に名前で頼むときに一致しなくなるため。"""
    fmt = fmt or DEFAULT_TITLE_FORMAT
    now = now or datetime.now()
    formats = _date_tools()
    topic = " ".join((topic or "").split())

    def replace_date(match):
        spec = match.group(2) or formats.get(match.group(1)) or _DEFAULT_DATE_FORMATS[match.group(1)]
        try:
            return now.strftime(spec)
        except ValueError:
            # Windows では未知の書式指定子で例外になる。snippets と同じく、その変数だけ
            # 元の記法のまま残す(会議名全体を捨てるほどではない)。
            return match.group(0)

    text = _DATE_RE.sub(replace_date, fmt).replace("{topic}", topic)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip(" \t　")


def needs_topic(fmt) -> bool:
    return "{topic}" in (fmt or DEFAULT_TITLE_FORMAT)


# ---------------------------------------------------------------------------
# エンジン(UIA を知らない)
# ---------------------------------------------------------------------------
def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value] if value != "" else []


def _expand(value, context):
    """手順の中の {title} / {camera} / {mic} を埋める。辞書とリストは潜って埋める。"""
    if isinstance(value, str):
        for key, replacement in context.items():
            value = value.replace("{" + key + "}", str(replacement))
        return value
    if isinstance(value, list):
        return [_expand(v, context) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, context) for k, v in value.items()}
    return value


def _classify_state(name, turn_on_names, turn_off_names):
    """ボタン名から、いまの状態を読む。'on' / 'off' / None(読めない)。

    ボタン名は「押すと何が起きるか」なので向きが逆になる。turn_on_names(押すと
    オンになる)に当たれば、いまは off。前方一致にしているのは、名前の後ろに
    ショートカットの表記((Ctrl+Shift+O))が付くため。両方に当たるときは長く
    一致したほうを採る(「マイクのミュート」と「マイクのミュートを解除」)。"""
    name = (name or "").strip()
    best, best_len = None, -1
    for candidate in _as_list(turn_on_names):
        if name.startswith(candidate) and len(candidate) > best_len:
            best, best_len = STATE_OFF, len(candidate)
    for candidate in _as_list(turn_off_names):
        if name.startswith(candidate) and len(candidate) > best_len:
            best, best_len = STATE_ON, len(candidate)
    return best


def _state_word(state):
    return {STATE_ON: "オン", STATE_OFF: "オフ"}.get(state, state)


class StepError(Exception):
    """手順が続けられないときに投げる。メッセージがそのまま通知に出る。"""


class Engine:
    """手順のリストを順に回す。UIA には backend を通してしか触らない。

    backend に要る口(UiaBackend と、テストの偽物が同じ形を持つ):
      windows()                 -> [TeamsWindow(hwnd, title), ...]
      launch()                  起動を頼む(待たない)
      find(hwnd, spec)          -> 要素(不透明な値) か None。毎回 WM_GETOBJECT で起こす
      press(element, prefer)    -> 使ったパターン名。押せなければ例外
      collapse(element)         開いたメニューを畳む(できなくてもよい)
      set_value(element, text)
      get_value(element)        -> str
      name_of(element)          -> str

    clock と sleep を差し替えられるのはテストのため(実時間を待たずに
    タイムアウトの経路を通せる)。"""

    def __init__(self, backend, steps, context, on_event=None,
                 poll=DEFAULT_POLL_SECONDS, clock=time.monotonic, sleep=time.sleep):
        self.backend = backend
        self.steps = [s for s in steps if isinstance(s, dict)]
        self.context = dict(context)
        self.on_event = on_event
        self.poll = poll
        self.clock = clock
        self.sleep = sleep
        self._remembered = {}     # remember_window で覚えた窓 {名前: hwnd}
        self._snapshot = None     # snapshot_windows の時点で居た窓の hwnd の集合

    # -- 進み具合 ------------------------------------------------------------
    def _emit(self, payload):
        if self.on_event is None:
            return
        try:
            self.on_event(payload)
        except Exception as e:  # noqa: BLE001  知らせ損ねても手順は止めない
            print(f"[teams_meeting] on_event 失敗: {e}", file=sys.stderr)

    # -- 本体 ----------------------------------------------------------------
    def run(self) -> dict:
        results = []
        active = [s for s in self.steps if not s.get("disabled")]
        self._emit({"event": "start", "title": self.context.get("title", ""),
                    "steps": [s.get("key") for s in active]})
        stopped = None
        for index, raw in enumerate(active):
            step = _expand(raw, self.context)
            key = step.get("key", f"step{index + 1}")
            label = step.get("label") or key
            self._emit({"event": "step_start", "key": key, "label": label,
                        "index": index + 1, "total": len(active)})
            started = self.clock()
            try:
                status, detail = self._run_step(step)
            except StepError as e:
                status, detail = "failed", str(e)
            except Exception as e:  # noqa: BLE001  知らない失敗でも報告して止まる
                status, detail = "failed", f"{type(e).__name__}: {e}"
            if status == "failed" and step.get("optional"):
                status = "skipped"
            result = {"key": key, "label": label, "status": status, "detail": detail,
                      "elapsed": round(self.clock() - started, 2)}
            results.append(result)
            self._emit(dict(result, event="step_end"))
            if status == "failed":
                stopped = result
                break
            delay = step.get("delay_after", 0.3 if status == "ok" else 0)
            if delay:
                self.sleep(float(delay))
        summary = {
            "event": "done",
            "ok": stopped is None,
            "title": self.context.get("title", ""),
            "stopped_at": stopped["key"] if stopped else None,
            "stopped_label": stopped["label"] if stopped else None,
            "stopped_detail": stopped["detail"] if stopped else None,
            "steps": results,
        }
        summary["transcript"] = transcript_outcome(summary)
        self._emit(summary)
        return summary

    def _run_step(self, step):
        action = step.get("action", "invoke")
        handler = {
            "ensure_app": self._do_ensure_app,
            "wait": self._do_wait,
            "invoke": self._do_press,
            "expand": self._do_press,
            "set_value": self._do_set_value,
            "ensure_state": self._do_ensure_state,
            "path": self._do_path,
        }.get(action)
        if handler is None:
            raise StepError(f"知らない action です: {action}")
        skip = step.get("skip_if_found")
        if skip:
            found = self._find_once(step.get("window"), skip)
            if found is not None:
                return "skipped", "既に目当ての画面が出ている"
        return handler(step)

    # -- 窓 ------------------------------------------------------------------
    def _windows(self, scope):
        """手順の window からの窓の候補。並びは「先に探す順」。

        meeting は、snapshot_windows の時点に居なかった窓と、会議名をタイトルに含む窓。
        タイトルだけに頼らないのは、長い会議名が窓のタイトルで省略される可能性を
        確かめていないため。新しく出た窓だけに頼らないのは、Teams が窓を使い回す
        可能性を否定できないため。両方を候補にして、新しく出たほうを先に探す。"""
        windows = list(self.backend.windows())
        if not scope or scope == "any":
            return windows
        if scope in self._remembered:
            return [w for w in windows if w.hwnd == self._remembered[scope]]
        if scope == "meeting":
            # 新しく出た窓を先に、次にタイトルが合う窓。main(今すぐ会議を押した窓)は
            # 外す: 会議を立てるとメイン窓にも同じ名前のチャットが開き、タイトルに
            # 会議名が入りうる。そこで「その他」などの同名ボタンを先に拾うと、会議と
            # 関係の無いものを押してしまう。ほかに候補が無いときだけ main も見る
            # (会議がメイン窓の中で開く版があっても、止まらずに済むように)。
            title = self.context.get("title") or ""
            main = self._remembered.get("main")
            fresh = []
            if self._snapshot is not None:
                fresh = [w for w in windows if w.hwnd not in self._snapshot]
            by_title = [w for w in windows if title and title in (w.title or "")
                        and w not in fresh]
            picked = [w for w in fresh + by_title if w.hwnd != main]
            return picked or [w for w in by_title if w.hwnd == main]
        # main をまだ覚えていない(手順を差し替えて meet_now を外した)ときは全部を見る。
        return windows

    def _find_once(self, scope, spec):
        """いまの時点で1回だけ探す。(要素, 窓) か None。"""
        for window in self._windows(scope):
            element = self.backend.find(window.hwnd, spec)
            if element is not None:
                return element, window
        return None

    def _poll(self, attempt, timeout):
        """attempt() が None 以外を返すまで繰り返す。(結果, 最後の例外)。

        attempt の中の例外は「まだ準備ができていない」とみなして続ける。画面の
        遷移中は、見つけた要素が次の瞬間に消えて COMError になることがあるため。
        comtypes.COMError は OSError の仲間ではないので、Exception で受けること。"""
        deadline = self.clock() + max(0.0, float(timeout))
        last_error = None
        while True:
            try:
                result = attempt()
                if result is not None:
                    return result, None
            except Exception as e:  # noqa: BLE001
                last_error = e
            if self.clock() >= deadline:
                return None, last_error
            self.sleep(self.poll)

    def _not_found(self, step, last_error, what=None):
        what = what or _describe(step.get("find"))
        message = f"{what} が {step.get('timeout', 10)}秒以内に見つかりません"
        if last_error is not None:
            message += f"（最後の失敗: {type(last_error).__name__}: {last_error}）"
        return StepError(message)

    def _remember(self, step, window):
        name = step.get("remember_window")
        if name:
            self._remembered[name] = window.hwnd

    # -- 各 action -----------------------------------------------------------
    def _do_ensure_app(self, step):
        if self.backend.windows():
            return "ok", "起動済み"
        try:
            self.backend.launch()
        except Exception as e:  # noqa: BLE001
            raise StepError(f"Teams を起動できません（{e}）")
        found, _err = self._poll(lambda: self.backend.windows() or None,
                                 step.get("timeout", 30))
        if not found:
            raise StepError(f"Teams の窓が {step.get('timeout', 30)}秒以内に出ません")
        return "ok", "起動しました"

    def _do_wait(self, step):
        found, err = self._poll(lambda: self._find_once(step.get("window"), step.get("find")),
                                step.get("timeout", 10))
        if found is None:
            raise self._not_found(step, err)
        self._remember(step, found[1])
        return "ok", f"「{found[1].title}」に出ました"

    def _do_press(self, step):
        prefer = "expand" if step.get("action") == "expand" else "invoke"
        if step.get("snapshot_windows"):
            self._snapshot = {w.hwnd for w in self.backend.windows()}

        def attempt():
            hit = self._find_once(step.get("window"), step.get("find"))
            if hit is None:
                return None
            used = self.backend.press(hit[0], prefer)
            return hit, used

        found, err = self._poll(attempt, step.get("timeout", 10))
        if found is None:
            raise self._not_found(step, err)
        (element, window), used = found
        self._remember(step, window)
        return "ok", f"押しました（{used}）"

    def _do_set_value(self, step):
        text = str(step.get("value", ""))
        found, err = self._poll(lambda: self._find_once(step.get("window"), step.get("find")),
                                step.get("timeout", 10))
        if found is None:
            raise self._not_found(step, err)
        element, window = found
        self._remember(step, window)
        # 読み戻して違っていたら入れ直す。ダイアログはフォーカスを取るので、別の窓で
        # 打っていた文字がこの欄に入りうる(実測で「susum」が混ざった)。
        last = None
        for attempt in range(SET_VALUE_ATTEMPTS):
            self.backend.set_value(element, text)
            try:
                last = self.backend.get_value(element)
            except Exception:  # noqa: BLE001  読めないなら入れたものを信じる
                return "ok", "入れました（読み戻せず）"
            if last == text:
                return "ok", "入れました" if attempt == 0 else f"入れ直しました（{attempt + 1}回目）"
        raise StepError(f"会議名が思った値になりません（いま: {last!r}）")

    def _do_ensure_state(self, step):
        want = str(step.get("want") or STATE_KEEP).lower()
        label = step.get("label") or step.get("key")
        if want not in (STATE_ON, STATE_OFF):
            return "skipped", "設定でそのまま"
        on_names, off_names = step.get("turn_on_names"), step.get("turn_off_names")
        found, err = self._poll(lambda: self._find_once(step.get("window"), step.get("find")),
                                step.get("timeout", 5))
        if found is None:
            raise self._not_found(step, err, what=f"{label}のボタン")
        element, _window = found
        name = self.backend.name_of(element)
        state = _classify_state(name, on_names, off_names)
        if state is None:
            raise StepError(f"{label}の状態をボタン名から読めません（{name!r}）。押していません")
        if state == want:
            return "ok", f"既に{_state_word(want)}"
        used = self.backend.press(element, "invoke")

        def switched():
            hit = self._find_once(step.get("window"), step.get("find"))
            if hit is None:
                return None
            now = _classify_state(self.backend.name_of(hit[0]), on_names, off_names)
            return now if now == want else None

        done, _err = self._poll(switched, STATE_VERIFY_SECONDS)
        if done is None:
            return "ok", f"押しました（{used}）が、{_state_word(want)}になったか確かめられません"
        return "ok", f"{_state_word(state)}→{_state_word(want)}にしました"

    def _do_path(self, step):
        """メニューを順にたどる。routes に別経路を並べてあれば、順に試す。

        途中で止まったら、開いたメニューを畳んでから次の経路へ移る(開きっぱなしの
        メニューが次の経路の要素を隠すことがあるため。畳めなくても進む)。"""
        routes = step.get("routes") or ([step["path"]] if step.get("path") else [])
        if not routes:
            raise StepError("path / routes が空です")
        failures = []
        for route in routes:
            opened, names = [], []
            for depth, sub in enumerate(route):
                window = sub.get("window", step.get("window"))
                timeout = step.get("timeout", 10) if depth == 0 else sub.get(
                    "timeout", PATH_SUB_TIMEOUT)
                prefer = sub.get("action", "invoke")

                def attempt(sub=sub, window=window, prefer=prefer):
                    hit = self._find_once(window, sub.get("find"))
                    if hit is None:
                        return None
                    return hit, self.backend.press(hit[0], prefer)

                found, _err = self._poll(attempt, timeout)
                if found is None:
                    failures.append(_describe(sub.get("find")))
                    for element in reversed(opened):
                        try:
                            self.backend.collapse(element)
                        except Exception:  # noqa: BLE001  畳めなくても次へ
                            pass
                    break
                (element, _w), _used = found
                # ログには実際に押した要素の名前を残す(候補の先頭ではなく)。業務版で
                # どの候補が当たったかが分かれば、設定を絞り込める。
                try:
                    names.append(self.backend.name_of(element) or _first_name(sub.get("find")))
                except Exception:  # noqa: BLE001  押したあとで要素が消えていることがある
                    names.append(_first_name(sub.get("find")))
                if prefer == "expand":
                    opened.append(element)
                self.sleep(float(sub.get("delay_after", 0.4)))
            else:
                return "ok", " → ".join(names)
        raise StepError("見つかりません: " + " / ".join(failures))


def _first_name(spec):
    spec = spec or {}
    for key in ("name", "name_prefix", "automation_id"):
        values = _as_list(spec.get(key))
        if values:
            return str(values[0])
    return "?"


def _describe(spec):
    """通知に出す「何を探したか」。長い候補リストは先頭だけ。"""
    return f"「{_first_name(spec)}」"


def transcript_outcome(summary) -> str:
    """文字起こしがどうなったか。'started' / 'not_found' / 'not_reached' / 'disabled'。

    not_reached は手前で止まったとき。not_found は探したが無かったとき(無料版は必ず
    これになる)。通知の文言をここで分けるので、呼び側は文字列を組み立てるだけでよい。"""
    for step in summary.get("steps", []):
        if step.get("key") == "transcript":
            return "started" if step.get("status") == "ok" else "not_found"
    if summary.get("stopped_at"):
        return "not_reached"
    return "disabled"


def describe_result(summary) -> str:
    """トーストに出す文。成功なら会議名と文字起こしの状況、失敗なら止まった手順。"""
    title = summary.get("title") or ""
    if summary.get("crashed"):
        return ("一人会議の立ち上げが途中で落ちました\n"
                "ここから先は Teams で手で続けてください")
    if not summary.get("ok"):
        return (f"一人会議の立ち上げが「{summary.get('stopped_label') or '?'}」で止まりました\n"
                f"{summary.get('stopped_detail') or ''}\n"
                "ここから先は Teams で手で続けてください")
    outcome = summary.get("transcript")
    if outcome == "started":
        tail = "文字起こしを開始しました"
    elif outcome == "disabled":
        tail = "文字起こしは手順に入っていません（必要なら手で開始してください）"
    else:
        tail = "文字起こしが見つかりませんでした。手で開始してください"
    return f"一人会議を開始しました\n会議名: {title}\n{tail}"


# ---------------------------------------------------------------------------
# UIA の口(子プロセスの中でだけ作る)
# ---------------------------------------------------------------------------
UIA_NAME_PROPERTY, UIA_AID_PROPERTY, UIA_TYPE_PROPERTY = 30005, 30011, 30003
INVOKE_PATTERN, VALUE_PATTERN, EXPAND_PATTERN, TOGGLE_PATTERN = 10000, 10002, 10005, 10015
WM_GETOBJECT, OBJID_CLIENT, SMTO_ABORTIFHUNG = 0x003D, 0xFFFFFFFC, 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
RENDER_CLASS = "Chrome_RenderWidgetHostHWND"

CONTROL_TYPES = {
    "Button": 50000, "CheckBox": 50002, "ComboBox": 50004, "Combo": 50004,
    "Edit": 50003, "Hyperlink": 50005, "Link": 50005, "ListItem": 50007,
    "Menu": 50009, "MenuItem": 50011, "RadioButton": 50013, "TabItem": 50019,
    "Text": 50020, "Custom": 50025, "Group": 50026, "Document": 50030,
    "SplitButton": 50031, "Window": 50032, "Pane": 50033,
}


class _Found:
    """見つけた要素と、それを取り出した親たち。

    【親も一緒に持つ理由】CLAUDE.md「COM オブジェクトを関数の外に出さない」。
    親(根の要素・FindAll の配列)を変数に受けずに子だけ返すと、親が先に解放されて
    0xC0000005 でプロセスごと即死したことがある。ここでは子プロセスなので常駐は
    巻き込まれないが、落ちれば立ち上げが途中で止まる。要素を使い終わるまで親を
    手放さない。"""

    __slots__ = ("element", "keep")

    def __init__(self, element, keep):
        self.element = element
        self.keep = keep


class UiaBackend:
    """Teams の窓を UIA で読み書きする。Engine から見た「薄い口」。

    COM はこのオブジェクトを作ったスレッドでだけ使うこと(アパートメントはスレッドに
    紐づく)。子プロセスのメインスレッドで作り、同じスレッドで回し、close で手放す。"""

    def __init__(self, app):
        self.app = dict(app)
        # user32 は自分専用に読み込む。ctypes.windll.user32 は全モジュールで共有される
        # オブジェクトなので、そこに argtypes を付けると他のモジュールが付けたものと
        # 食い合う(同じ関数に別の型を付けた側が勝つ)。
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._declare()
        self._render_cache = {}
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as uia_mod
        self.UIA = uia_mod
        self.uia = comtypes.client.CreateObject(uia_mod.CUIAutomation,
                                                interface=uia_mod.IUIAutomation)
        self.true_cond = self.uia.CreateTrueCondition()

    def _declare(self):
        """ctypes は argtypes / restype を必ず付ける(CLAUDE.md)。HWND は 64bit で、
        省くと int に切り詰められてアクセス違反になる。"""
        u, k = self.user32, self.kernel32
        self._enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        u.EnumWindows.argtypes = [self._enum_proc, ctypes.c_void_p]
        u.EnumWindows.restype = ctypes.c_bool
        u.EnumChildWindows.argtypes = [ctypes.c_void_p, self._enum_proc, ctypes.c_void_p]
        u.EnumChildWindows.restype = ctypes.c_bool
        u.IsWindowVisible.argtypes = [ctypes.c_void_p]
        u.IsWindowVisible.restype = ctypes.c_bool
        u.IsWindow.argtypes = [ctypes.c_void_p]
        u.IsWindow.restype = ctypes.c_bool
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        u.GetWindowThreadProcessId.restype = ctypes.c_ulong
        u.SendMessageTimeoutW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
            ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
        ]
        u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        k.OpenProcess.restype = ctypes.c_void_p
        k.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        k.QueryFullProcessImageNameW.restype = ctypes.c_bool
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle.restype = ctypes.c_bool

    def close(self):
        """COM への参照を、作ったのと同じスレッドで手放す(copilot_loop.Copilot.close
        と同じ理由。GC 任せだと解放が別の場所へ先送りされる)。"""
        self.true_cond = None
        self.uia = None

    # -- 窓 ------------------------------------------------------------------
    def _text(self, func, hwnd, size):
        buf = ctypes.create_unicode_buffer(size)
        func(hwnd, buf, size)
        return buf.value

    def _exe_of(self, hwnd):
        """窓を持つプロセスの exe 名。psutil を使わない(このモジュールは常駐からも
        import されるので、COM を内部で使う psutil を持ち込まない方針に揃える)。"""
        pid = ctypes.c_ulong()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value)
        finally:
            self.kernel32.CloseHandle(handle)

    def windows(self):
        want_exe = (self.app.get("process_name") or "").lower()
        want_class = self.app.get("window_class") or ""
        found = []

        def on_window(hwnd, _l):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            if want_class and self._text(self.user32.GetClassNameW, hwnd, 256) != want_class:
                return True
            if want_exe and self._exe_of(hwnd).lower() != want_exe:
                return True
            found.append(TeamsWindow(hwnd, self._text(self.user32.GetWindowTextW, hwnd, 512)))
            return True

        self.user32.EnumWindows(self._enum_proc(on_window), None)
        return found

    def launch(self):
        """起動を頼むだけで待たない(待つのは Engine)。候補を順に試す。

        前面に出すのは OS と Teams の仕事で、こちらは SetForegroundWindow を呼ばない。"""
        errors = []
        for target in _as_list(self.app.get("launch")):
            try:
                if str(target).lower().startswith("shell:"):
                    subprocess.Popen(["explorer.exe", target])
                else:
                    os.startfile(target)
                return
            except OSError as e:
                errors.append(f"{target}: {e}")
        raise OSError("; ".join(errors) or "起動のしかたが設定されていません")

    def _render_child(self, hwnd):
        """レンダラの窓を再帰的に探す(EnumChildWindows が直接の子しか返さない環境が
        ある。copilot_loop._find_render_child と同じ)。窓ごとに覚えておく。"""
        if hwnd in self._render_cache:
            return self._render_cache[hwnd]
        found = {"h": None}

        def walk(parent):
            def on_child(child, _l):
                if found["h"] is not None:
                    return False
                if self._text(self.user32.GetClassNameW, child, 256) == RENDER_CLASS:
                    found["h"] = child
                    return False
                walk(child)
                return found["h"] is None
            self.user32.EnumChildWindows(parent, self._enum_proc(on_child), None)

        walk(hwnd)
        self._render_cache[hwnd] = found["h"]
        return found["h"]

    def wake(self, hwnd):
        """WM_GETOBJECT を投げてアクセシビリティツリーを起こす(uia_probe の
        wake_accessibility と同じ)。Chromium(WebView2)は支援技術を検出するまで木を
        作らない。メイン窓とレンダラの両方に投げる。既に起きていれば無害。"""
        out = ctypes.c_size_t()
        for target in (hwnd, self._render_child(hwnd)):
            if target:
                self.user32.SendMessageTimeoutW(
                    target, WM_GETOBJECT, 0, ctypes.c_ssize_t(OBJID_CLIENT),
                    SMTO_ABORTIFHUNG, 1000, ctypes.byref(out))

    # -- 要素 ----------------------------------------------------------------
    def find(self, hwnd, spec):
        if not spec or not self.user32.IsWindow(hwnd):
            return None
        self.wake(hwnd)
        root = self.uia.ElementFromHandle(ctypes.c_void_p(hwnd))
        return self._find_in(root, spec, [root])

    def _type_ok(self, element, types):
        return not types or element.CurrentControlType in types

    def _find_in(self, base, spec, keep):
        """base の子孫から spec に合うものを1つ。aid → name → name_prefix の順。

        within があれば、先にその要素を探して、その中だけを探す。**within が
        見つからなければ、外側へ広げて探すことはしない**(招待ダイアログの「閉じる」を
        窓のタイトルバーの「閉じる」と取り違えないため)。"""
        within = spec.get("within")
        if within:
            container = self._find_in(base, within, keep)
            if container is None:
                return None
            base, keep = container.element, container.keep + [container.element]
        types = [CONTROL_TYPES.get(t, t) for t in _as_list(spec.get("control_type"))]
        scope = self.UIA.TreeScope_Descendants
        for prop, values in ((UIA_AID_PROPERTY, spec.get("automation_id")),
                             (UIA_NAME_PROPERTY, spec.get("name"))):
            for value in _as_list(values):
                cond = self.uia.CreatePropertyCondition(prop, str(value))
                array = base.FindAll(scope, cond)
                for i in range(array.Length):
                    element = array.GetElement(i)
                    try:
                        if self._type_ok(element, types):
                            return _Found(element, keep + [cond, array])
                    except Exception:  # noqa: BLE001  消えかけの要素は飛ばす
                        continue
        prefixes = _as_list(spec.get("name_prefix"))
        if prefixes:
            array = base.FindAll(scope, self.true_cond)
            for prefix in prefixes:
                for i in range(array.Length):
                    element = array.GetElement(i)
                    try:
                        name = (element.CurrentName or "").strip()
                        if name.startswith(prefix) and self._type_ok(element, types):
                            return _Found(element, keep + [array])
                    except Exception:  # noqa: BLE001
                        continue
        return None

    def _pattern(self, element, pattern_id, interface):
        pattern = element.GetCurrentPattern(pattern_id)
        if not pattern:
            return None
        return pattern.QueryInterface(interface)

    def press(self, found, prefer="invoke"):
        """押す。prefer の順にパターンを試し、使えたものの名前を返す。

        「その他」は InvokePattern を持たず ExpandCollapse で開く(実測)。逆に
        ExpandCollapse を持っていても Expand が COMError になる要素もあった
        (「言語と音声」)。だから1つに決め打ちせず、失敗したら次を試す。"""
        uia = self.UIA
        order = {
            "invoke": ("invoke", "toggle", "expand"),
            "expand": ("expand", "invoke", "toggle"),
        }.get(prefer, ("invoke", "toggle", "expand"))
        errors = []
        for kind in order:
            try:
                if kind == "invoke":
                    p = self._pattern(found.element, INVOKE_PATTERN, uia.IUIAutomationInvokePattern)
                    if p:
                        p.Invoke()
                        return "Invoke"
                elif kind == "toggle":
                    p = self._pattern(found.element, TOGGLE_PATTERN, uia.IUIAutomationTogglePattern)
                    if p:
                        p.Toggle()
                        return "Toggle"
                else:
                    p = self._pattern(found.element, EXPAND_PATTERN,
                                      uia.IUIAutomationExpandCollapsePattern)
                    if p:
                        p.Expand()
                        return "Expand"
            except Exception as e:  # noqa: BLE001  COMError は OSError ではない
                errors.append(f"{kind}: {e}")
        raise RuntimeError("押せません（" + ("; ".join(errors) or "押すパターンが無い") + "）")

    def collapse(self, found):
        p = self._pattern(found.element, EXPAND_PATTERN, self.UIA.IUIAutomationExpandCollapsePattern)
        if p:
            p.Collapse()

    def set_value(self, found, text):
        p = self._pattern(found.element, VALUE_PATTERN, self.UIA.IUIAutomationValuePattern)
        if not p:
            raise RuntimeError("書き込めません（ValuePattern が無い）")
        p.SetValue(text)

    def get_value(self, found):
        p = self._pattern(found.element, VALUE_PATTERN, self.UIA.IUIAutomationValuePattern)
        if not p:
            raise RuntimeError("読めません（ValuePattern が無い）")
        return p.CurrentValue

    def name_of(self, found):
        return (found.element.CurrentName or "").strip()


# ---------------------------------------------------------------------------
# 常駐から起こす口(COM に触らない)
# ---------------------------------------------------------------------------
def spawn(title, on_event=None):
    """このファイルを子プロセスで起こし、進捗を on_event に流す。proc を返す。

    on_event は読み役のスレッドから呼ばれる。Qt を直接触らないこと(シグナル経由で
    メインスレッドへ渡す)。子が "done" を言わずに終わったら(落ちた・殺された)、
    読み役が代わりに crashed の done を流す。呼び側は「必ず done が1回来る」前提で
    状態を片付けてよい。"""
    exe = sys.executable
    # pythonw.exe には標準出力が無い。進捗を受け取りたいので python.exe を使い、
    # コンソール窓は CREATE_NO_WINDOW で出さない(agent_loop.spawn と同じ)。
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[: -len("pythonw.exe")] + "python.exe"
    argv = [exe, str(_HERE / "teams_meeting.py"), "--title", title, "--emit-events"]
    proc = subprocess.Popen(
        argv, cwd=str(_HERE),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    def reader():
        got_done = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("event") == "done":
                    got_done = True
                if on_event is not None:
                    on_event(payload)
        except Exception as e:  # noqa: BLE001
            print(f"[teams_meeting] 出力の読み取りに失敗: {e}", file=sys.stderr)
        finally:
            try:
                code = proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                code = None
            if not got_done and on_event is not None:
                try:
                    on_event({"event": "done", "ok": False, "crashed": True,
                              "title": title, "exit_code": code, "steps": [],
                              "stopped_label": "子プロセス",
                              "stopped_detail": f"終了コード {code}",
                              "transcript": "not_reached"})
                except Exception as e:  # noqa: BLE001
                    print(f"[teams_meeting] 終了の知らせに失敗: {e}", file=sys.stderr)

    threading.Thread(target=reader, name="teams-meeting-reader", daemon=True).start()
    return proc


# ---------------------------------------------------------------------------
# 子プロセスの入口
# ---------------------------------------------------------------------------
def run(title, app_settings=None, on_event=None) -> dict:
    """子プロセスの本体。UIA の口を作って手順を回し、まとめを返す。"""
    config = load_config(app_settings)
    backend = UiaBackend(config["app"])
    try:
        engine = Engine(backend, config["steps"],
                        {"title": title, "camera": config["camera"], "mic": config["mic"]},
                        on_event=on_event, poll=config["poll_seconds"])
        return engine.run()
    finally:
        backend.close()


def check(app_settings=None) -> int:
    """読むだけの確かめ。押さない・入れない・起動しない。

    Teams の窓が見つかるか、各手順の要素がいま見えているかを1行ずつ出す。会議の前に
    走らせれば、見えるのは「今すぐ会議」まで(会議の中のものは会議を始めないと出ない)。"""
    config = load_config(app_settings)
    backend = UiaBackend(config["app"])
    try:
        windows = backend.windows()
        print(f"[窓] {config['app'].get('process_name')} … {len(windows)}枚")
        for w in windows:
            print(f"   hwnd={w.hwnd}  title={w.title!r}")
        if not windows:
            print("   Teams が起動していないか、窓が隠れています（ここでは起動しません）")
            return 1
        context = {"title": "(未定)", "camera": config["camera"], "mic": config["mic"]}
        print()
        print("[手順ごとの要素] ○=いま見えている  ・=いまは見えない（会議の中のものは正常）")
        for raw in config["steps"]:
            step = _expand(raw, context)
            specs = []
            if step.get("find"):
                specs.append(step["find"])
            for route in step.get("routes") or []:
                specs.extend(sub.get("find") for sub in route if sub.get("find"))
            if not specs:
                print(f"   -  {step.get('key'):<13} {step.get('label')}")
                continue
            for spec in specs:
                hit = None
                for w in windows:
                    try:
                        if backend.find(w.hwnd, spec) is not None:
                            hit = w
                            break
                    except Exception:  # noqa: BLE001
                        continue
                mark = "○" if hit else "・"
                where = f"  （{hit.title}）" if hit else ""
                print(f"   {mark}  {step.get('key'):<13} {_first_name(spec)}{where}")
        return 0
    finally:
        backend.close()


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Teams の一人会議を立ち上げる。")
    parser.add_argument("--title", help="会議名。省略すると title_format から作る")
    parser.add_argument("--topic", default="", help="--title を省略したときのテーマ")
    parser.add_argument("--emit-events", action="store_true",
                        help="進捗を1行1件の JSON で標準出力へ流す（常駐が読む）")
    parser.add_argument("--check", action="store_true",
                        help="読むだけ。窓と各手順の要素が見えるかを出す（押さない）")
    parser.add_argument("--list", action="store_true",
                        help="settings.json を重ねたあとの手順を JSON で出す")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    import settings as settings_module
    app_settings = settings_module.load_settings()

    if args.list:
        config = load_config(app_settings)
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0
    if args.check:
        return check(app_settings)

    title = args.title or format_title(load_config(app_settings)["title_format"], args.topic)

    def emit_line(payload):
        try:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()   # 常駐が待っているので溜めない
        except (OSError, ValueError):
            pass

    def print_line(payload):
        if payload.get("event") == "step_end":
            print(f"  {payload['status']:<8} {payload['label']}  {payload.get('detail', '')}")

    summary = run(title, app_settings, on_event=emit_line if args.emit_events else print_line)
    if not args.emit_events:
        print(describe_result(summary))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
