# muse_post.py
# Meta の AI エージェント Muse のデスクトップアプリへ、依頼文を1行投稿する CLI。
# 「Claude を窓口にして Muse に仕事を渡す」連携の、Muse 側の入口。
#
#   python muse_post.py status
#   python muse_post.py post --message "..." [--current-chat] [--dry-run]
#   python muse_post.py post --message-file handoff\xxx.txt
#   python muse_post.py wait --repo R:\claude\tray-tools --branch muse/xxx
#
# 出力は標準出力に JSON 1つ。終了コード 0=成功 / 1=中止(安全確認で止めた・
# 見つからない・タイムアウト) / 2=引数の誤り。
#
# 【なぜ常駐(main.py)でも MCP でもなく単独スクリプトなのか】
# CLAUDE.md「外から叩ける口」: 足してよいのは常駐でなければできないことだけ。
# Muse への投稿は常駐の状態を何も使わないので、呼ぶ側(Claude Code)がこの
# スクリプトを直接叩けば足りる。常駐を経由させても口を通る危険が増えるだけ。
# 同じ理由で PySide6 も import しない(Qt の import だけで1秒近くかかる)。
#
# 【なぜ UIA なのか】
# Muse には外部 API が無く、入口はチャット欄だけ。キー送信やクリックで書くと
# フォーカスを奪い、陽太さんが別の窓で作業していたらそちらに文字が飛ぶ。
# UIA の ValuePattern / InvokePattern は要素を名指しで書き・押すので、
# 前面化もフォーカス移動も要らない(copilot_loop と同じ考え方)。
# **キー送信・マウス操作・SetForegroundWindow・SetFocus は使わない。**
#
# 【いちばん大事な約束: 人の入力を上書きしない】
# 投稿欄が空でなければ、陽太さんが何か書きかけている。SetValue はそれを黙って
# 消してしまうので、空でない限り何もしない。新しいサイドチャットを作る前にも
# 確かめる(作ると書きかけの下書きごと画面が切り替わる)。
#
# 【Muse の画面が変わったら】
# 窓の探し方もボタン名も MUSE_PROFILE にまとめてある。直すのはそこだけ。
# 調べ直すには tools/uia_probe.py が使える。
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

import comtypes
import comtypes.client

import copilot_loop
from copilot_loop import (
    INVOKE_PATTERN, MIN_AWAKE_DESCENDANTS, OBJID_CLIENT, SMTO_ABORTIFHUNG,
    VALUE_PATTERN, WM_GETOBJECT, _ensure_uia, find_window,
)

VERSION = "20261104-01"

# --- Muse アプリの見え方(2026-11-04 実測) ----------------------------------
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


def check_new_chat_ready(s):
    """新しいサイドチャットを作った直後、投稿してよいか。よければ None。"""
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
    文面を送らないため。"""
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
# UIA で Muse を扱う
# =========================================================================
class MuseNotFound(RuntimeError):
    pass


class Muse:
    """Muse アプリの窓を1つ掴んで、読み書きする。

    copilot_loop.Copilot を使わないのは、あちらが投稿欄を AutomationId で探す作りで、
    AutomationId の無い Muse に合わないため。窓探し・UIA の初期化は同じものを使う。

    COM オブジェクトは属性として持つ(関数の外に子だけ出すと即解放されて落ちる)。
    使い終わったら close() で、掴んだのと同じスレッドで手放すこと。"""

    def __init__(self, profile=MUSE_PROFILE):
        self.profile = profile
        main, render = find_window(profile)
        if not main:
            raise MuseNotFound("Muse の窓が見つからない")
        self.hwnd_main, self.hwnd_render = main, render
        self.uia = None
        self.true_cond = None
        self._wake()
        # 窓が見つかってから初めて COM を触る(見つからなければ COM を初期化しない)。
        uia_mod = _ensure_uia()
        self.uia = comtypes.client.CreateObject(
            uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation)
        self.true_cond = self.uia.CreateTrueCondition()

    def close(self):
        """COM への参照を、掴んだのと同じスレッドで明示的に手放す(Copilot.close と同じ)。"""
        self.true_cond = None
        self.uia = None

    def window_title(self):
        return copilot_loop._title_of(self.hwnd_main)

    def _wake(self):
        """Chromium のアクセシビリティツリーを起こす合図。投げるだけで待たない。

        起こさないと子孫が窓枠の26個しか見えない(起こすと282〜571個)。"""
        out = copilot_loop.ctypes.c_size_t()
        copilot_loop.user32.SendMessageTimeoutW(
            copilot_loop.ctypes.c_void_p(self.hwnd_render or self.hwnd_main), WM_GETOBJECT, 0,
            copilot_loop.ctypes.c_ssize_t(OBJID_CLIENT), SMTO_ABORTIFHUNG, 1000,
            copilot_loop.ctypes.byref(out),
        )

    def _descendants(self):
        """毎回取り直す。画面が変わると前に取った要素は無効になるため。"""
        root = self.uia.ElementFromHandle(copilot_loop.ctypes.c_void_p(self.hwnd_main))
        desc = root.FindAll(copilot_loop.UIA.TreeScope_Descendants, self.true_cond)
        return root, desc

    def wait_awake(self, timeout=AWAKE_TIMEOUT, poll=POLL_SEC):
        """木が組み上がるまで待つ。組み上がったか(bool)を返す。"""
        deadline = time.time() + max(0.0, timeout)
        while True:
            _root, desc = self._descendants()
            if desc.Length >= MIN_AWAKE_DESCENDANTS:
                return True
            if time.time() >= deadline:
                return False
            self._wake()  # 1回目を取りこぼすことがある。投げ直しても害はない
            time.sleep(poll)

    def scan(self):
        """木を1回歩いて (要約, 要素) を返す。

        要素は役目ごとのリスト {'input', 'send', 'new_chat', 'open_list'}。
        押す・書くのは、この直後に、この要素に対してだけ行う(古い要素は無効)。"""
        profile = self.profile
        inputs = set(_names(profile, "input_name"))
        send = set(_names(profile, "send_button"))
        new_chat = set(_names(profile, "new_chat_button"))
        suffixes = _names(profile, "open_list_suffix")
        refs = {"input": [], "send": [], "new_chat": [], "open_list": []}
        items = []
        _root, desc = self._descendants()
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                ctype = el.CurrentControlType
                name = (el.CurrentName or "").strip()
            except Exception:  # noqa: BLE001  要素が消えた直後など(COMError は OSError でない)
                continue
            items.append((ctype, name))
            if ctype == CONTROL_EDIT and name in inputs:
                refs["input"].append(el)
            elif ctype == CONTROL_BUTTON:
                if name in send:
                    refs["send"].append(el)
                elif name in new_chat:
                    refs["new_chat"].append(el)
                elif any(name.endswith(s) for s in suffixes):
                    refs["open_list"].append(el)
        values = [self.read_value(el) for el in refs["input"]]
        return summarize(items, values, profile), refs

    def poll(self, predicate, timeout, poll=POLL_SEC):
        """predicate(要約) が真になるまで scan を繰り返す。(要約, 要素, 成否) を返す。

        走査中に画面が切り替わると COM が例外を投げることがある。上限までは
        取り直し続ける(1回の失敗で諦めると、切り替わりの最中に当たっただけで止まる)。"""
        deadline = time.time() + max(0.0, timeout)
        s, refs = None, None
        while True:
            try:
                s, refs = self.scan()
                if predicate(s):
                    return s, refs, True
            except Exception:  # noqa: BLE001
                pass
            if time.time() >= deadline:
                if s is None:
                    s, refs = self.scan()
                return s, refs, False
            time.sleep(poll)

    @staticmethod
    def read_value(el):
        """ValuePattern の値。読めなければ None。"""
        try:
            pattern = el.GetCurrentPattern(VALUE_PATTERN)
            if not pattern:
                return None
            value = pattern.QueryInterface(copilot_loop.UIA.IUIAutomationValuePattern)
            return value.CurrentValue
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def set_value(el, text):
        pattern = el.GetCurrentPattern(VALUE_PATTERN)
        if not pattern:
            raise RuntimeError("投稿欄に書き込めない(ValuePattern が無い)")
        value = pattern.QueryInterface(copilot_loop.UIA.IUIAutomationValuePattern)
        value.SetValue(text)

    @staticmethod
    def invoke(el):
        pattern = el.GetCurrentPattern(INVOKE_PATTERN)
        if not pattern:
            raise RuntimeError("ボタンを押せない(InvokePattern が無い)")
        invoke = pattern.QueryInterface(copilot_loop.UIA.IUIAutomationInvokePattern)
        invoke.Invoke()


# =========================================================================
# サブコマンド
# =========================================================================
def cmd_status(_args):
    try:
        muse = Muse()
    except MuseNotFound as e:
        return 1, {"found": False, "busy": None, "chat_title": None, "input_empty": None,
                   "can_new_chat": None, "descendants": 0, "reason": str(e)}
    try:
        awake = muse.wait_awake()
        s, _refs = muse.scan()
        return 0, {
            "found": True,
            "busy": s["busy"],
            "chat_title": s["chat_title"],
            "input_empty": s["input_empty"],
            "can_new_chat": s["new_chat"] > 0 or s["open_list"] > 0,
            "descendants": s["descendants"],
            "awake": awake,
            "window_title": muse.window_title(),
            "detail": public_summary(s),
        }
    finally:
        muse.close()


def _dry_run_plan(s, current_chat, message):
    """--dry-run のときに「実行したら何をするか」を並べる。UI には触らない。"""
    plan = []
    if current_chat:
        plan.append(f"表示中のチャット『{s['chat_title'] or '(名前不明)'}』に投稿する")
    else:
        if s["new_chat"] == 0:
            plan.append("一覧を開くボタン(…チャットとサイドチャットを開く)を Invoke する")
            plan.append(f"『新しいサイドチャット』が出るのを最大 {OPEN_LIST_TIMEOUT:g} 秒待つ")
        plan.append("『新しいサイドチャット』を Invoke する")
        plan.append(f"返信/リアクションのボタンが0個になるのを最大 {NEW_CHAT_TIMEOUT:g} 秒待つ"
                    "(ならなければ中止)")
    plan.append("投稿欄が空であることを確かめ直す")
    plan.append(f"投稿欄に SetValue する({len(message)} 文字)")
    plan.append(f"『送信』が出て、読み戻した値が一致するのを最大 {SEND_APPEAR_TIMEOUT:g} 秒待つ")
    plan.append("『送信』を Invoke する")
    plan.append(f"投稿欄が空に戻るのを最大 {CLEAR_TIMEOUT:g} 秒待つ")
    plan.append(f"チャット名を最大 {TITLE_TIMEOUT:g} 秒待って読む")
    return plan


def _restore_input(muse, refs, message):
    """送らずに中止するとき、自分が書いた文字だけを消して元に戻す。

    残すと次の投稿が「投稿欄が空でない」で止まり、陽太さんにも書きかけに見える。
    ただし中身が自分の書いたものと一致するときだけ消す(その間に人が打った分は
    消さない)。戻せたか(bool)を返す。"""
    try:
        if len(refs["input"]) != 1:
            return False
        el = refs["input"][0]
        if not _same_text(muse.read_value(el), message):
            return False
        muse.set_value(el, "")
        return True
    except Exception:  # noqa: BLE001
        return False


def cmd_post(args, message):
    result = {"ok": False, "posted_at": None, "chat_title": None,
              "new_chat": not args.current_chat, "reason": None,
              "dry_run": bool(args.dry_run), "steps": []}
    steps = result["steps"]
    try:
        muse = Muse()
    except MuseNotFound as e:
        result["reason"] = str(e)
        return 1, result
    try:
        if not muse.wait_awake():
            steps.append("木が組み上がらないまま進む(下の確認で止まるはず)")
        s, refs = muse.scan()
        result["found"] = public_summary(s)
        reason = check_initial(s, args.current_chat)

        if args.dry_run:
            result["plan"] = [] if reason else _dry_run_plan(s, args.current_chat, message)
            result["ok"] = reason is None
            result["reason"] = reason
            result["chat_title"] = s["chat_title"]
            return (0 if reason is None else 1), result

        if reason:
            result["reason"] = reason
            return 1, result

        if not args.current_chat:
            if s["new_chat"] == 0:
                muse.invoke(refs["open_list"][0])
                steps.append("一覧を開いた")
                s, refs, ok = muse.poll(lambda x: x["new_chat"] >= 1, OPEN_LIST_TIMEOUT)
                if not ok:
                    result["reason"] = "一覧を開いたが『新しいサイドチャット』が出ない"
                    return 1, result
                if s["new_chat"] != 1:
                    result["reason"] = f"『新しいサイドチャット』が {s['new_chat']} 個ある"
                    return 1, result
            muse.invoke(refs["new_chat"][0])
            steps.append("新しいサイドチャットを作った")
            s, refs, ok = muse.poll(lambda x: check_new_chat_ready(x) is None, NEW_CHAT_TIMEOUT)
            if not ok:
                result["reason"] = check_new_chat_ready(s) or "新しいチャットの準備を確かめられない"
                return 1, result

        title_before = s["chat_title"]
        reason = check_before_set(s)
        if reason:
            result["reason"] = reason
            return 1, result

        muse.set_value(refs["input"][0], message)
        steps.append("投稿欄に書いた")
        s, refs, ok = muse.poll(lambda x: check_before_invoke(x, message) is None,
                                SEND_APPEAR_TIMEOUT)
        if not ok:
            result["reason"] = check_before_invoke(s, message) or "送信の準備を確かめられない"
            if _restore_input(muse, refs, message):
                steps.append("送らずに投稿欄を空に戻した")
            return 1, result

        muse.invoke(refs["send"][0])
        result["posted_at"] = now_iso()
        steps.append("送信を押した")

        s, refs, ok = muse.poll(lambda x: x["inputs"] == 1 and x["input_empty"] is True,
                                CLEAR_TIMEOUT)
        if not ok:
            result["reason"] = "送信を押したが投稿欄が空に戻らない(送れたか不明。二重投稿に注意)"
            result["chat_title"] = s["chat_title"]
            return 1, result

        placeholders = _names(muse.profile, "placeholder_titles")
        new_chat = not args.current_chat
        s, refs, settled = muse.poll(
            lambda x: title_settled(title_before, x["chat_title"], new_chat, placeholders,
                                    message),
            TITLE_TIMEOUT)
        result["chat_title"] = s["chat_title"]
        result["chat_title_settled"] = settled
        result["ok"] = True
        return 0, result
    except Exception as e:  # noqa: BLE001  ここまでに何を押したか(steps)を残して返す
        result["reason"] = f"例外: {type(e).__name__}: {e}"
        return 1, result
    finally:
        muse.close()


def cmd_wait(args):
    """ブランチが remote に現れるまで待つ。UIA は使わない(git だけ)。"""
    if not os.path.isdir(args.repo):
        return 2, {"ok": False, "reason": f"リポジトリが無い: {args.repo}"}
    if args.interval <= 0 or args.timeout < 0:
        return 2, {"ok": False, "reason": "--interval は正、--timeout は0以上"}
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # 認証を対話で聞かれて固まらないように
    start = time.time()
    last_error = None
    while True:
        try:
            proc = subprocess.run(
                ["git", "-C", args.repo, "ls-remote", "--heads", "origin", args.branch],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=GIT_TIMEOUT, env=env)
            if proc.returncode == 0:
                sha = parse_ls_remote(proc.stdout, args.branch)
                last_error = None
                # ブランチが現れただけでは終わっていない。Muse はブランチを main の先頭から
                # 先に作り(承認1回目)、結果は後から push する(承認2回目)。2026-11-04 の
                # 2回目の試運転で、ブランチ出現時は main と同じコミットで中身が空だった。
                # --path があれば、そのファイルがブランチに載るまで待ち続ける。
                if sha and args.path:
                    fetched = subprocess.run(
                        ["git", "-C", args.repo, "fetch", "-q", "origin",
                         f"+refs/heads/{args.branch}:refs/remotes/origin/{args.branch}"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=GIT_TIMEOUT, env=env)
                    if fetched.returncode != 0:
                        last_error = (f"git fetch の終了コード {fetched.returncode}: "
                                      f"{redact(fetched.stderr)}")
                        sha = None
                    else:
                        has = subprocess.run(
                            ["git", "-C", args.repo, "cat-file", "-e",
                             f"refs/remotes/origin/{args.branch}:{args.path}"],
                            capture_output=True, timeout=GIT_TIMEOUT, env=env)
                        if has.returncode != 0:
                            sha = None  # ブランチはあるが結果ファイルがまだ
                if sha:
                    return 0, {"ok": True, "appeared_after_sec": round(time.time() - start, 1),
                               "sha": sha, "branch": args.branch, "path": args.path}
            else:
                last_error = f"git の終了コード {proc.returncode}: {redact(proc.stderr)}"
        except subprocess.TimeoutExpired:
            last_error = f"git が {GIT_TIMEOUT} 秒で返らない"
        elapsed = time.time() - start
        if elapsed >= args.timeout:
            out = {"ok": False, "reason": "timeout", "waited_sec": round(elapsed, 1),
                   "branch": args.branch}
            if last_error:
                out["last_error"] = last_error
            return 1, out
        time.sleep(min(args.interval, max(0.0, args.timeout - elapsed)))


# =========================================================================
# 入口
# =========================================================================
class _JsonArgParser(argparse.ArgumentParser):
    """引数の誤りも JSON 1つで返す(呼ぶ側が標準出力だけを読めば済むように)。"""

    def error(self, message):
        _emit({"ok": False, "reason": f"引数の誤り: {message}"})
        sys.exit(2)


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_parser():
    p = _JsonArgParser(prog="muse_post.py", description="Muse アプリへ UIA で投稿する")
    p.add_argument("--version", action="store_true", help="版を表示する")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="状態を読むだけ")
    post = sub.add_parser("post", help="依頼文を投稿する")
    src = post.add_mutually_exclusive_group(required=True)
    src.add_argument("--message")
    src.add_argument("--message-file")
    post.add_argument("--current-chat", action="store_true",
                      help="新しいサイドチャットを作らず、表示中のチャットに投稿する")
    post.add_argument("--dry-run", action="store_true", help="UI を一切変えずに計画だけ返す")
    post.add_argument("--allow-multiline", action="store_true",
                      help="改行入りの投稿文を許す(送信の挙動は未検証)")
    wait = sub.add_parser("wait", help="remote にブランチが現れるまで待つ")
    wait.add_argument("--repo", required=True)
    wait.add_argument("--branch", required=True)
    wait.add_argument("--timeout", type=float, default=3000)
    wait.add_argument("--interval", type=float, default=30)
    wait.add_argument("--path", help="このファイルがブランチに載るまで待つ(例: handoff/<id>.result.md)")
    return p


def read_message(args):
    """投稿文を取る。ファイルは UTF-8(BOM 可)で、末尾の改行だけ落とす
    (エディタが必ず付ける最後の改行で「複数行」扱いになるのを避けるため)。"""
    if args.message is not None:
        return args.message
    with open(args.message_file, encoding="utf-8-sig") as f:
        return f.read().rstrip("\r\n")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    if args.version:
        _emit({"version": VERSION})
        return 0
    if not args.cmd:
        _emit({"ok": False, "reason": "サブコマンド(status / post / wait)を指定する"})
        return 2
    try:
        if args.cmd == "status":
            code, out = cmd_status(args)
        elif args.cmd == "post":
            try:
                message = read_message(args)
            except OSError as e:
                _emit({"ok": False, "reason": f"--message-file を読めない: {e}"})
                return 2
            err = message_error(message, args.allow_multiline)
            if err:
                _emit({"ok": False, "reason": err})
                return 2
            code, out = cmd_post(args, message)
        else:
            code, out = cmd_wait(args)
    except Exception as e:  # noqa: BLE001  COMError は OSError でないので広く受ける
        code, out = 1, {"ok": False, "reason": f"例外: {type(e).__name__}: {e}"}
    _emit(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
