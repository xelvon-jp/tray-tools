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
    # \u672c\u547d\u306e\u898b\u5206\u3051\u65b9(2026-11-12)\u3002\u7a93\u306e AppUserModelID \u304c PWA \u306f 'muse.ai-\u2026!App'\u3001
    # \u666e\u901a\u306e Edge \u306f 'MSEdge'\u3002\u53d6\u308c\u305f\u7a93\u306f\u3053\u308c\u3060\u3051\u3067\u6c7a\u3081\u3001\u53d6\u308c\u306a\u3044\u7a93\u3060\u3051\u30bf\u30a4\u30c8\u30eb\u3067\u898b\u308b\u3002
    "app_id_prefix": "muse.ai-",
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
CONTROL_TEXT, CONTROL_DATA_ITEM, CONTROL_GROUP = 50020, 50007, 50026

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
              "open_list": 0, "message_buttons": 0,
              "approval_cards": 0, "approval_banner": 0}
    button_names = []
    for ctype, name in items:
        name = (name or "").strip()
        if ctype == CONTROL_EDIT and name in inputs:
            counts["inputs"] += 1
        if ctype == CONTROL_GROUP and name == APPROVAL_CARD_NAME:
            counts["approval_cards"] += 1
        if ctype != CONTROL_BUTTON:
            continue
        button_names.append(name)
        if name in send:
            counts["send"] += 1
        if name in busy:
            counts["busy"] += 1
        if name == APPROVAL_BANNER:
            counts["approval_banner"] += 1
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
        "approval_cards": counts["approval_cards"],
        "approval_banner": counts["approval_banner"] > 0,
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


def check_initial(s, current_chat, allow_busy=False):
    """着手してよいか。よければ None、だめなら理由。何も押す前に呼ぶ。

    新しいチャットを作る場合でも投稿欄が空であることを求める。作ると画面が
    切り替わり、書きかけの下書きが見えなくなる(消えるかもしれない)ため。
    busy は、表示中のチャットに投稿するときだけ止める。新しいチャットは別物なので
    作ってから確かめ直す(check_new_chat_ready)。"""
    reason = _input_reason(s)
    if reason:
        return reason
    if current_chat:
        if s["busy"] and not allow_busy:
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


def check_before_set(s, allow_busy=False):
    """SetValue の直前。よければ None。
    allow_busy: 2026-11-13 陽太さん許可。やり取りが長く詰まったときは、作業中でも割り込んで投稿してよい。"""
    if s["busy"] and not allow_busy:
        return "作業中(『停止』がある)"
    return _input_reason(s)


def _same_text(a, b):
    if a is None or b is None:
        return False
    return a.replace("\r\n", "\n") == b.replace("\r\n", "\n")


def check_before_invoke(s, message, allow_busy=False):
    """送信ボタンを押す直前。よければ None。

    読み戻した値が送った文字列と一致することを求める。React が SetValue を
    取りこぼしたり、陽太さんが同時に打鍵して混ざったりしたときに、意図と違う
    文面を送らないため。
    busy もここで見直す(20261105-01 #3)。SetValue の待ちの間に表示中の
    チャットが作業中になったら、押さない(allow_busy のときは押す。送信ボタンが出なければ押さない)。"""
    if s["busy"] and not allow_busy:
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


# =========================================================================
# 承認カードの解析と判定(20261105-02。実機で測った形に基づく)
# =========================================================================
# カードは表示中のチャットの中に Group(Name が下記)として出て、その子孫に
# 「ラベル → 値」の順で Text が並ぶ。同じラベルが DataItem としても重複して
# 出るが、そちらは見ない(Text だけを見る)。
APPROVAL_CARD_NAME = "GitHubの承認リクエスト"
APPROVAL_ACTION = "Push files to GitHub"
APPROVE_BUTTON = "許可する"
DENY_BUTTON = "許可しない"
# 承認待ちの間、窓の上部に出るボタン。カードが別チャットにある目印。
APPROVAL_BANNER = "Muse、承認が必要です"
APPROVE_TIMEOUT = 6.0        # 許可する を押してからカードが消えるまで
CHAT_SWITCH_TIMEOUT = 6.0    # 一覧で選んでからそのチャットが表示されるまで

# ラベルとして知っているもの。これ以外が来たら「未知」として押さない側に倒す。
_APPROVAL_SCALAR_LABELS = ("Action", "Branch or ref", "Repository owner", "Repository")
_APPROVAL_FILES_LABEL = re.compile(r"Files · (\d+) · (File path|Content)")
# ラベルではなく単独で出る、知っている注記。
_APPROVAL_NOTES = ("Additional request details are not shown.",
                   "プレビューは省略されています")
# カード冒頭の問いかけ。実機(2026-11-05)では「Museに⁨GitHubアカウントでこのアクションを実行⁩を
# 許可しますか？」で、⁨⁩(U+2068/U+2069 の方向制御文字)が挟まる。中身は固定の定型文なので、
# 制御文字を除いた全文の一致で注記扱いにする(これが未知扱いで watch が毎回止まっていた)。
_APPROVAL_HEADER = "MuseにGitHubアカウントでこのアクションを実行を許可しますか？"
_BIDI_MARKS = dict.fromkeys(map(ord, "⁦⁧⁨⁩‎‏"), None)
# ラベル「Content」の次の Text はコミットメッセージ(ファイル番号の付かない Content)。
_APPROVAL_COMMIT_LABEL = "Content"


def split_approval_cards(items):
    """窓の子孫の並びから、承認カードごとの部分並びを切り出す。

    カードの Group が現れた所から、次のカードの Group の手前までが1枚ぶん。
    カードの外の要素は捨てる。"""
    cards = []
    current = None
    for ctype, name in items:
        name = (name or "").strip()
        if ctype == CONTROL_GROUP and name == APPROVAL_CARD_NAME:
            current = [(ctype, name)]
            cards.append(current)
            continue
        if current is not None:
            current.append((ctype, name))
    return cards


def parse_approval_card(items):
    """承認カード1枚ぶんの (ControlType, Name) の並びを辞書にする。

    Text の並びを「ラベル → 値」として読む。知らないラベル・余った文字列は
    unknown に集める(判定で押さない理由になる)。DataItem は重複なので無視。"""
    card = {"action": None, "branch": None, "owner": None, "repo": None,
            "files": [], "contents": [], "notes": [], "unknown": [],
            "duplicates": [], "approve_buttons": 0, "deny_buttons": 0,
            "files_complete": True}
    scalar_keys = {"Action": "action", "Branch or ref": "branch",
                   "Repository owner": "owner", "Repository": "repo"}
    file_paths = {}
    file_contents = {}
    pending = None  # 次の Text を値として受けるラベル(文字列 or ("files", 番号, 種類))

    def take_value(value):
        nonlocal pending
        label, pending = pending, None
        if isinstance(label, str):
            key = scalar_keys[label]
            if card[key] is not None and card[key] != value:
                card["duplicates"].append(label)
            card[key] = value
            return
        _tag, idx, kind = label
        target = file_paths if kind == "File path" else file_contents
        if idx in target and target[idx] != value:
            card["duplicates"].append(f"Files · {idx} · {kind}")
        target[idx] = value

    for ctype, name in items:
        name = (name or "").strip()
        if ctype == CONTROL_GROUP and name == APPROVAL_CARD_NAME:
            continue
        if ctype == CONTROL_BUTTON:
            if name == APPROVE_BUTTON:
                card["approve_buttons"] += 1
            elif name == DENY_BUTTON:
                card["deny_buttons"] += 1
            continue
        if ctype != CONTROL_TEXT:
            continue  # DataItem の重複などは見ない
        if not name and pending is None:
            continue  # 中身の無い Text(実機で1つ出た)。情報が無いので判定に使わない
        if pending is None and name in _APPROVAL_SCALAR_LABELS:
            pending = name
            continue
        m = _APPROVAL_FILES_LABEL.fullmatch(name) if pending is None else None
        if m:
            pending = ("files", int(m.group(1)), m.group(2))
            continue
        if pending is None and (name in _APPROVAL_NOTES
                                or name.translate(_BIDI_MARKS) == _APPROVAL_HEADER):
            card["notes"].append(name)
            continue
        if pending is None and name == _APPROVAL_COMMIT_LABEL:
            pending = "commit"
            continue
        if pending == "commit":
            pending = None
            card["commit_message"] = name
            continue
        if pending is not None:
            take_value(name)
            continue
        card["unknown"].append(name)
    if pending is not None:
        card["unknown"].append(f"{pending}(値が無い)")
    indices = sorted(file_paths)
    card["files"] = [file_paths[i] for i in indices]
    card["contents"] = [file_contents[i] for i in sorted(file_contents)]
    card["files_complete"] = indices == list(range(1, len(indices) + 1))
    return card


def approval_decision(card, expect):
    """このカードの『許可する』を押してよいか。よければ None、だめなら理由。

    expect は {"owner", "repo", "branch", "allow_paths"}。
    迷う要素が1つでもあれば押さない(『許可しない』も押さない。人に残す)。"""
    branch = expect.get("branch") or ""
    if not branch.startswith("muse/"):
        return "--branch は muse/ で始まらなければならない"
    if card["unknown"]:
        return f"未知の項目がある: {card['unknown'][0]}"
    if card["duplicates"]:
        return f"同じ項目が複数ある: {card['duplicates'][0]}"
    for key, label in (("action", "Action"), ("branch", "Branch or ref"),
                       ("owner", "Repository owner"), ("repo", "Repository")):
        if card[key] is None:
            return f"{label} が読めない"
    if card["action"] != APPROVAL_ACTION:
        return f"Action が想定と違う: {card['action']}"
    if card["branch"] != branch:
        return f"Branch or ref が一致しない: {card['branch']}"
    if card["owner"] != expect.get("owner"):
        return f"Repository owner が一致しない: {card['owner']}"
    if card["repo"] != expect.get("repo"):
        return f"Repository が一致しない: {card['repo']}"
    if not card["files"]:
        return "ファイルが1件も無い"
    if not card["files_complete"]:
        return "ファイルの一覧が揃っていない(番号の抜けがある)"
    allow = set(expect.get("allow_paths") or [])
    for f in card["files"]:
        if f not in allow:
            return f"許可されていないパスが含まれている: {f}"
    if card["approve_buttons"] != 1 or card["deny_buttons"] != 1:
        return ("『許可する』『許可しない』の数が想定と違う"
                f"(許可する {card['approve_buttons']} / 許可しない {card['deny_buttons']})")
    return None


def public_card(card):
    """出力用。ファイルの内容(先頭)は出さず、パスと件数だけにする。"""
    return {"action": card["action"], "branch": card["branch"],
            "owner": card["owner"], "repo": card["repo"],
            "files": card["files"], "contents_count": len(card["contents"])}


# =========================================================================
# チャット一覧での名前の一致(20261105-02、post --chat 用)
# =========================================================================
# 一覧の Button の Name はチャット名そのもの。未読があると後ろに印が付く。
CHAT_UNREAD_SUFFIXES = (" 未読のアップデート", " 未読のチャット")


def match_chat_buttons(button_names, title):
    """Name がチャット名と完全一致、または未読の印付きのものを集める。"""
    out = []
    for raw in button_names:
        name = (raw or "").strip()
        if name == title or any(name == title + s for s in CHAT_UNREAD_SUFFIXES):
            out.append(name)
    return out
