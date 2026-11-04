# muse_post_logic.py
# muse_post.py の判断ロジック(純関数)と、Muse アプリの見え方の定義。
#
# 【なぜ分けたか】(20261105-01 #11)
# もとは muse_post.py が冒頭で comtypes / copilot_loop を読み込んでいたため、
# 純関数のテストまで Windows + UIA 依存の環境でしか import できなかった
# (Linux では comtypes が無く、テストが起動すらしない)。
# ここには判断だけを置き、comtypes・copilot_loop は一切 import しない。
# UIA で実際に窓を扱う側は muse_post.py に残してある。
import datetime
import re

# --- Muse アプリの見え方(2026-11-04 実測。muse_post.py から移設) -----------
# 名前はすべてリスト。copilot_loop の BUILTIN_PROFILES と同じ理由で、1つの完全一致に
# 賭けると文言が少し変わっただけで「見つからない」になり、理由も分からないため。
MUSE_PROFILE = {
    "name": "muse",
    # 実体は Edge の PWA。exe とクラスだけでは普通の Edge の窓と区別がつかない。
    "process_name": "msedge.exe",
    "window_class": "Chrome_WidgetWin_1",
    # タイトルは「Muse - Chat — Muse」。"Muse" だけで探すと、Obsidian の窓
    # (ノート名に Muse が入っていることがある)に当たる。em dash(U+2014)付きの
    # 末尾で探し、さらに exe で絞る。find_window は大文字小文字を区別しない部分一致。
    "window_title_contains": "\u2014 Muse",
    "render_class": "Chrome_RenderWidgetHostHWND",
    # 投稿欄。AutomationId が無いので Name で探す。Name「検索」の Edit も居るので
    # Name の一致は必須(Edit というだけで拾うと検索欄に書いてしまう)。
    "input_name": ["メッセージ"],
    # 入力欄が空のときは存在しない。文字が入ると出る。
    "send_button": ["送信"],
    # 作業中に投稿欄の横に出る。
    "busy_button": ["停止"],
    # サイドバーのチャット一覧が開いているときだけ見える。
    "new_chat_button": ["新しいサイドチャット"],
    # 一覧を開くボタン。Name は「<チャット名> [未読のチャット] チャットとサイドチャットを開く」。
    # 末尾一致で探し、前の部分からチャット名を取る。
    "open_list_suffix": ["チャットとサイドチャットを開く"],
    "unread_marker": ["未読のチャット"],
    # 会話のメッセージに付くボタン。作りたての空のチャットには1つも無い。
    "message_buttons": ["返信", "リアクションする"],
    # サイドチャットの名前は最初の投稿から Muse が付ける。付く前の仮の名前は
    # 未実測なので空。分かったら足す(足すとチャット名の待ちがそれを読み飛ばす)。
    "placeholder_titles": [],
}

CONTROL_BUTTON, CONTROL_EDIT = 50000, 50004

# --- 待ち時間(すべてポーリング。上限だけをここで決める) ---------------------
POLL_SEC = 0.25
AWAKE_TIMEOUT = 6.0        # 木が組み上がるまで(実測は1秒未満)
OPEN_LIST_TIMEOUT = 4.0    # 一覧を開いてから「新しいサイドチャット」が出るまで
NEW_CHAT_TIMEOUT = 6.0     # 新しいサイドチャットが空の状態で表示されるまで
SEND_APPEAR_TIMEOUT = 4.0  # SetValue してから「送信」が出るまで
CLEAR_TIMEOUT = 6.0        # 送信してから投稿欄が空に戻るまで
TITLE_TIMEOUT = 20.0       # 送信してから Muse がチャット名を付けるまで

# wait サブコマンドの git 1回ぶんの上限。認証待ちで固まらないよう短めに切る。
GIT_TIMEOUT = 60

# アクセシビリティツリーが「起きている」とみなす子孫の数。
# copilot_loop.MIN_AWAKE_DESCENDANTS(30)と同じ基準。あちらを import すると
# UIA 依存が戻ってしまうので、値をここにも持つ。変えるときは両方を直すこと。
MIN_AWAKE_DESCENDANTS = 30

# 空とみなす文字。React のエディタは空でも幅ゼロの文字を抱えていることがある。
_BLANK_CHARS = "\u200b\ufeff"


# =========================================================================
# 判断ロジック(純関数。UIA 無しでテストできるようにここに集める)
# =========================================================================
def _names(profile, key):
    """プロファイルの項目を必ずリストで返す(文字列1つでも書けるように)。"""
    value = profile.get(key) or []
    return [value] if isinstance(value, str) else list(value)


def is_blank(value):
    """投稿欄の中身が「空」か。読めなかった(None)ものは空とみなさない。

    空白だけ・幅ゼロ文字だけは空とする。上書きして困るものではないし、
    React が空の欄に残す文字で「入力中」と誤判定して止まるのを避けるため。"""
    if value is None:
        return False
    return value.strip().strip(_BLANK_CHARS).strip() == ""


def chat_title_from_names(button_names, profile=MUSE_PROFILE):
    """「…チャットとサイドチャットを開く」ボタンの Name から、表示中のチャット名を取る。

    例: 'Tray-tools ルール調査の試運転 未読のチャット チャットとサイドチャットを開く'
        → 'Tray-tools ルール調査の試運転'
    ボタンが無い(メインチャット表示中・一覧が開いている)、または名前部分が空なら None。"""
    for raw in button_names:
        name = (raw or "").strip()
        for suffix in _names(profile, "open_list_suffix"):
            if not name.endswith(suffix):
                continue
            title = name[: -len(suffix)].strip()
            for marker in _names(profile, "unread_marker"):
                if title.endswith(marker):
                    title = title[: -len(marker)].strip()
            return title or None
    return None


def summarize(items, input_values, profile=MUSE_PROFILE):
    """木を1回歩いた結果を、判断に要る数だけに畳む。

    items        [(ControlType, Name), ...] 子孫ぜんぶ
    input_values 投稿欄(Name 一致の Edit)それぞれの値。読めなかったものは None
    """
    send = set(_names(profile, "send_button"))
    busy = set(_names(profile, "busy_button"))
    new_chat = set(_names(profile, "new_chat_button"))
    msg = set(_names(profile, "message_buttons"))
    suffixes = _names(profile, "open_list_suffix")
    inputs = set(_names(profile, "input_name"))

    counts = {"inputs": 0, "send": 0, "busy": 0, "new_chat": 0,
              "open_list": 0, "message_buttons": 0}
    button_names = []
    for ctype, name in items:
        name = (name or "").strip()
        if ctype == CONTROL_EDIT and name in inputs:
            counts["inputs"] += 1
        if ctype != CONTROL_BUTTON:
            continue
        button_names.append(name)
        if name in send:
            counts["send"] += 1
        if name in busy:
            counts["busy"] += 1
        if name in new_chat:
            counts["new_chat"] += 1
        if name in msg:
            counts["message_buttons"] += 1
        if any(name.endswith(s) for s in suffixes):
            counts["open_list"] += 1

    value = input_values[0] if len(input_values) == 1 else None
    return {
        "descendants": len(items),
        "inputs": counts["inputs"],
        "input_value": value,
        "input_empty": is_blank(value) if len(input_values) == 1 and value is not None else None,
        "send": counts["send"],
        "busy": counts["busy"] > 0,
        "new_chat": counts["new_chat"],
        "open_list": counts["open_list"],
        "message_buttons": counts["message_buttons"],
        "chat_title": chat_title_from_names(button_names, profile),
    }


def public_summary(s):
    """出力用。投稿欄の中身(書きかけの下書きかもしれない)は出さず、長さだけ出す。"""
    out = {k: v for k, v in s.items() if k != "input_value"}
    value = s.get("input_value")
    out["input_length"] = None if value is None else len(value)
    return out


def _input_reason(s):
    """投稿欄まわりの共通の確認。問題が無ければ None。"""
    if s["descendants"] < MIN_AWAKE_DESCENDANTS:
        return f"アクセシビリティツリーが起きていない(子孫 {s['descendants']} 個)"
    if s["inputs"] == 0:
        return "投稿欄(Name='メッセージ' の Edit)が見つからない"
    if s["inputs"] > 1:
        return f"投稿欄が {s['inputs']} 個ある(どれに書くか決められない)"
    if s["input_value"] is None:
        return "投稿欄の中身を読めない(ValuePattern が無い)"
    if not s["input_empty"]:
        return "投稿欄が空でない(入力中かもしれないので上書きしない)"
    return None


def check_initial(s, current_chat):
    """着手してよいか。よければ None、だめなら理由。何も押す前に呼ぶ。

    新しいチャットを作る場合でも投稿欄が空であることを求める。作ると画面が
    切り替わり、書きかけの下書きが見えなくなる(消えるかもしれない)ため。
    busy は、表示中のチャットに投稿するときだけ止める。新しいチャットは別物なので
    作ってから確かめ直す(check_new_chat_ready)。"""
    reason = _input_reason(s)
    if reason:
        return reason
    if current_chat:
        if s["busy"]:
            return "表示中のチャットは作業中(『停止』がある)"
        return None
    if s["new_chat"] == 0 and s["open_list"] == 0:
        return "新しいサイドチャットを作れない(ボタンも一覧を開くボタンも無い)"
    if s["new_chat"] > 1:
        return f"『新しいサイドチャット』が {s['new_chat']} 個ある"
    if s["new_chat"] == 0 and s["open_list"] > 1:
        return f"一覧を開くボタンが {s['open_list']} 個ある"
    return None


def is_new_chat_empty(s):
    """作りたてのサイドチャットか(会話のメッセージに付くボタンが1つも無いか)。"""
    return s["message_buttons"] == 0


def view_signature(s):
    """画面の同一性を比べるための組(20261105-01 #1)。

    「新しいサイドチャット」を押す前と後でこれが同じなら、まだ画面が
    切り替わっていない(scan が切り替わり前の状態を拾っている)とみなす。
    逆に、作った新しいチャットと元のチャットがたまたま全部同じ値になる場合は
    「切り替わっていない」側に倒れて時間切れで止まる(誤投稿より安全)。"""
    return (s["chat_title"], s["descendants"], s["message_buttons"])


def check_new_chat_ready(s, before_sig=None):
    """新しいサイドチャットを作った直後、投稿してよいか。よければ None。

    before_sig に Invoke 直前の view_signature を渡すと、それと同じ画面の
    間は「まだ切り替わっていない」として待たせる(#1)。表示中のチャット自体が
    空の作りたてだと、状態だけの判定では切り替わり前に通ってしまい、元の
    チャットへ投稿してしまうため。"""
    if before_sig is not None and view_signature(s) == before_sig:
        return "まだ新しいチャットに切り替わっていない(Invoke 前と同じ画面)"
    if not is_new_chat_empty(s):
        return f"新しいチャットが空になっていない(返信等のボタンが {s['message_buttons']} 個)"
    if s["busy"]:
        return "新しいチャットが作業中になっている(『停止』がある)"
    return _input_reason(s)


def check_before_set(s):
    """SetValue の直前。よければ None。"""
    if s["busy"]:
        return "作業中(『停止』がある)"
    return _input_reason(s)


def _same_text(a, b):
    if a is None or b is None:
        return False
    return a.replace("\r\n", "\n") == b.replace("\r\n", "\n")


def check_before_invoke(s, message):
    """送信ボタンを押す直前。よければ None。

    読み戻した値が送った文字列と一致することを求める。React が SetValue を
    取りこぼしたり、陽太さんが同時に打鍵して混ざったりしたときに、意図と違う
    文面を送らないため。
    busy もここで見直す(20261105-01 #3)。SetValue の待ちの間に表示中の
    チャットが作業中になったら、押さない。"""
    if s["busy"]:
        return "作業中(『停止』がある)"
    if s["inputs"] != 1:
        return "投稿欄を見失った"
    if not _same_text(s["input_value"], message):
        return "投稿欄の中身が送るはずの文字列と一致しない"
    if s["send"] == 0:
        return "『送信』ボタンが出ない"
    if s["send"] > 1:
        return f"『送信』ボタンが {s['send']} 個ある"
    return None


def title_settled(before, now, new_chat, placeholders=(), message=None):
    """チャット名が決まったとみなしてよいか。

    新しいチャットでは、投稿前に見えていた名前(まだ付いていない仮の名前)から
    変わるのを待つ。表示中のチャットへの投稿では名前は変わらないので、取れれば足りる。

    投稿文そのものと同じ名前も「まだ」とみなす。2026-11-04 の実測で、投稿直後の
    サイドチャットの名前が依頼文の全文になっていた(要約した名前に置き換わるのかは
    未確認)。置き換わらなければ上限まで待って、その名前を settled=false で返す。"""
    if not now or now in placeholders:
        return False
    if message and now.strip() == message.strip():
        return False
    if new_chat:
        return now != before
    return True


def message_error(message, allow_multiline):
    """投稿文として受け付けられないなら理由を返す(終了コード 2 にする)。"""
    if message is None or not message.strip():
        return "投稿文が空"
    if ("\n" in message or "\r" in message) and not allow_multiline:
        return ("投稿文に改行が入っている。改行入りの送信は未検証なので、"
                "送るなら --allow-multiline を付ける")
    return None


def parse_ls_remote(output, branch):
    """`git ls-remote --heads origin NAME` の出力から、そのブランチの sha を取る。

    ls-remote のパターンは末尾一致なので、NAME が 'x' なら 'refs/heads/foo/x' も
    返ってくる。refs/heads/NAME と完全に一致する行だけを採る。"""
    want = "refs/heads/" + branch
    for line in (output or "").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1] == want and re.fullmatch(r"[0-9a-f]{40,64}", parts[0]):
            return parts[0]
    return None


def redact(text):
    """git のエラーには remote URL(認証情報入りのことがある)が混ざる。URL を伏せる。"""
    text = re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+", "<url>", text or "")
    text = re.sub(r"\S+@[\w.-]+:\S+", "<url>", text)  # git@github.com:owner/repo 形式
    return text.strip()[-300:]


def now_iso():
    """ローカル時刻(JST)の ISO 8601。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
