# muse_post.py
# Meta の AI エージェント Muse のデスクトップアプリへ、依頼文を1行投稿する CLI。
# 「Claude を窓口にして Muse に仕事を渡す」連携の、Muse 側の入口。
#
#   python muse_post.py status
#   python muse_post.py post --message "..." [--current-chat] [--dry-run]
#   python muse_post.py post --message "..." --chat "チャット名" [--expect-text "依頼id"]
#   python muse_post.py post --message "..." --desk tray-tools --request-id 依頼id [--new-desk]
#   python muse_post.py desk [--set tray-tools --chat "チャット名" --last-id 依頼id]
#   python muse_post.py post --message-file handoff\xxx.txt
#   python muse_post.py approve --owner X --repo Y --branch muse/xxx --allow-path muse_post.py [--dry-run]
#   python muse_post.py watch --repo-dir R:\claude\tray-tools --owner X --repo Y --branch muse/xxx --path handoff\xxx.result.md --allow-path muse_post.py
#   python muse_post.py wait --repo R:\claude\tray-tools --branch muse/xxx --path handoff\xxx.result.md
#
# 出力は標準出力に JSON 1つ。終了コード 0=成功 / 1=中止(安全確認で止めた・
# 見つからない・タイムアウト・人に残す) / 2=引数の誤り。
#
# approve は承認カードの内容が指定とすべて一致するときだけ『許可する』を押す。
# watch は結果ファイルの到着と承認カードをスクリプトのループで見張り、Claude が
# ポーリングで起き続けないようにするもの。判定は muse_post_logic.py の純関数。
#
# 判断ロジック(純関数)と Muse アプリの見え方の定義は muse_post_logic.py にある
# (20261105-01 #11)。あちらは comtypes・copilot_loop を読まないので、判断の
# テストは UIA の無い環境(Linux など)でも回る。ここに残るのは UIA の実操作と
# サブコマンドの進行だけ。
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
# 窓の探し方もボタン名も MUSE_PROFILE(muse_post_logic.py)にまとめてある。
# 直すのはそこだけ。調べ直すには tools/uia_probe.py が使える。
import argparse
import json
import os
import subprocess
import sys
import time

from muse_post_logic import (
    APPROVE_BUTTON, APPROVE_TIMEOUT, AWAKE_TIMEOUT, CHAT_SWITCH_TIMEOUT,
    APPROVAL_CARD_NAME, CLEAR_TIMEOUT, CONTROL_BUTTON, CONTROL_EDIT, CONTROL_GROUP,
    CONTROL_TEXT, GIT_TIMEOUT,
    MUSE_PROFILE, NEW_CHAT_TIMEOUT, OPEN_LIST_TIMEOUT, POLL_SEC,
    SEND_APPEAR_TIMEOUT, TITLE_TIMEOUT, _names, _same_text, approval_decision,
    check_before_invoke, check_before_set, check_initial, check_new_chat_ready,
    is_blank, match_chat_buttons, message_error, now_iso, parse_approval_card,
    parse_ls_remote, public_card, public_summary, redact, split_approval_cards,
    summarize, title_settled, view_signature,
)

# UIA 依存はここでまとめて読む。読めない環境(Linux の VM など)でも、判断の
# テストや wait のために import だけは通るようにする(#11)。実際に窓を扱う
# Muse の生成では、読めなかった理由を添えて止まる。
try:
    import comtypes
    import comtypes.client

    import copilot_loop
    from copilot_loop import (
        INVOKE_PATTERN, MIN_AWAKE_DESCENDANTS, OBJID_CLIENT, SMTO_ABORTIFHUNG,
        VALUE_PATTERN, WM_GETOBJECT, _ensure_uia, find_window,
    )
    _UIA_IMPORT_ERROR = None
except ImportError as _e:  # ModuleNotFoundError を含む
    comtypes = None
    copilot_loop = None
    INVOKE_PATTERN = MIN_AWAKE_DESCENDANTS = OBJID_CLIENT = None
    SMTO_ABORTIFHUNG = VALUE_PATTERN = WM_GETOBJECT = None
    _ensure_uia = find_window = None
    _UIA_IMPORT_ERROR = _e

VERSION = "20261105-05"


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
        if _UIA_IMPORT_ERROR is not None:
            raise MuseNotFound(
                f"UIA 依存(comtypes / copilot_loop)を読み込めない: {_UIA_IMPORT_ERROR}")
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

        要素は役目ごとのリスト {'input', 'send', 'new_chat', 'open_list'} に、
        全 Button の (Name, 要素) を 'buttons'、全 Text の Name を 'texts'、
        子孫の (ControlType, Name) の並びそのものを 'items' に足してある
        (承認カードの解析と、チャット一覧での選択に使う)。"""
        profile = self.profile
        inputs = set(_names(profile, "input_name"))
        send = set(_names(profile, "send_button"))
        new_chat = set(_names(profile, "new_chat_button"))
        suffixes = _names(profile, "open_list_suffix")
        refs = {"input": [], "send": [], "new_chat": [], "open_list": [],
                "buttons": [], "texts": [], "items": []}
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
                refs["buttons"].append((name, el))
                if name in send:
                    refs["send"].append(el)
                elif name in new_chat:
                    refs["new_chat"].append(el)
                elif any(name.endswith(s) for s in suffixes):
                    refs["open_list"].append(el)
            elif ctype == CONTROL_TEXT:
                refs["texts"].append(name)
            elif ctype == CONTROL_GROUP and name == APPROVAL_CARD_NAME:
                refs.setdefault("cards", []).append(el)
        refs["items"] = items
        values = [self.read_value(el) for el in refs["input"]]
        return summarize(items, values, profile), refs

    def card_contents(self, card_el):
        """承認カードの Group の子孫だけを歩き、((ControlType, Name) の並び, [(Name, Button)]) を返す。"""
        items = [(CONTROL_GROUP, APPROVAL_CARD_NAME)]
        buttons = []
        desc = card_el.FindAll(copilot_loop.UIA.TreeScope_Descendants, self.true_cond)
        for i in range(desc.Length):
            el = desc.GetElement(i)
            try:
                ctype = el.CurrentControlType
                name = (el.CurrentName or "").strip()
            except Exception:  # noqa: BLE001  COMError は OSError でない
                continue
            items.append((ctype, name))
            if ctype == CONTROL_BUTTON:
                buttons.append((name, el))
        return items, buttons

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


def _poll_scan(muse, predicate, timeout):
    """predicate(要約, 要素) が真になるまで scan を繰り返す。Muse.poll と同じ作法。

    要素(ボタン一覧など)も見て判定したいときに使う(post --chat の切り替え)。"""
    deadline = time.time() + max(0.0, timeout)
    s, refs = None, None
    while True:
        try:
            s, refs = muse.scan()
            if predicate(s, refs):
                return s, refs, True
        except Exception:  # noqa: BLE001
            pass
        if time.time() >= deadline:
            if s is None:
                s, refs = muse.scan()
            return s, refs, False
        time.sleep(POLL_SEC)


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
            "approval_cards": s["approval_cards"],
            "approval_banner": s["approval_banner"],
            "detail": public_summary(s),
        }
    finally:
        muse.close()


def _dry_run_plan(s, current_chat, message, chat=None, expect_text=None):
    """--dry-run のときに「実行したら何をするか」を並べる。UI には触らない。"""
    plan = []
    if chat:
        plan.append("チャット一覧を開く")
        plan.append(f"『{chat}』と完全一致(未読の印つきを含む)のボタンをちょうど1つ見つけて Invoke する")
        if expect_text:
            plan.append(f"切り替え後の画面に『{expect_text}』を含む Text があることを確かめる")
        plan.append(f"表示中のチャット『{chat}』に投稿する")
    elif current_chat:
        plan.append(f"表示中のチャット『{s['chat_title'] or '(名前不明)'}』に投稿する")
    else:
        if s["new_chat"] == 0:
            plan.append("一覧を開くボタン(…チャットとサイドチャットを開く)を Invoke する")
            plan.append(f"『新しいサイドチャット』が出るのを最大 {OPEN_LIST_TIMEOUT:g} 秒待つ")
        plan.append("『新しいサイドチャット』を Invoke する")
        plan.append(f"画面が Invoke 前と変わり、返信/リアクションのボタンが0個になるのを"
                    f"最大 {NEW_CHAT_TIMEOUT:g} 秒待つ(ならなければ中止)")
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
        if not refs or len(refs["input"]) != 1:
            return False
        el = refs["input"][0]
        if not _same_text(muse.read_value(el), message):
            return False
        muse.set_value(el, "")
        return True
    except Exception:  # noqa: BLE001
        return False


def cmd_post(args, message, muse_factory=None):
    """投稿の進行。muse_factory はテスト用の差し替え口(偽の Muse を渡せる)。"""
    chat_arg = getattr(args, "chat", None)
    expect_text = getattr(args, "expect_text", None)
    to_current = bool(args.current_chat or chat_arg)
    result = {"ok": False, "posted_at": None, "chat_title": None,
              "new_chat": not to_current, "reason": None,
              "dry_run": bool(args.dry_run), "steps": []}
    if chat_arg:
        result["chat"] = chat_arg
    steps = result["steps"]
    try:
        muse = (muse_factory or Muse)()
    except MuseNotFound as e:
        result["reason"] = str(e)
        return 1, result
    s, refs = None, None
    wrote = False           # SetValue で自分の文面を書いた後か
    send_attempted = False  # 送信 Invoke に入った後か(例外でも送れたか不明になる)
    leftovers = []          # 取り消せない UI 側の副作用(#6)。理由に明示する

    def note_suffix():
        return f"({'・'.join(leftovers)}が残っている)" if leftovers else ""

    try:
        if not muse.wait_awake():
            steps.append("木が組み上がらないまま進む(下の確認で止まるはず)")
        s, refs = muse.scan()
        result["found"] = public_summary(s)
        # --chat でも、切り替え前に表示中のチャットの下書きを守る(同じ確認を使う)
        reason = check_initial(s, True) if chat_arg else check_initial(s, args.current_chat)

        if args.dry_run:
            result["plan"] = [] if reason else _dry_run_plan(
                s, args.current_chat, message, chat=chat_arg, expect_text=expect_text)
            result["ok"] = reason is None
            result["reason"] = reason
            result["chat_title"] = s["chat_title"]
            return (0 if reason is None else 1), result

        if reason:
            result["reason"] = reason
            return 1, result

        # 表示中のチャットに前回の依頼 id が見えていれば、それが目的のチャット。
        # 一覧を開かずにそのまま投稿する(窓口チャットは Muse の作業後もそのまま
        # 表示されていることが多く、名前が読めなくても続けられるようにするため)。
        here = bool(expect_text) and any(
            expect_text in t for t in (refs.get("texts") or []))
        if args.current_chat and expect_text and not here:
            result["reason"] = (f"表示中のチャットに『{expect_text}』が見つからない"
                                "(別のチャットの可能性があるので投稿しない)")
            return 1, result
        if chat_arg and here:
            steps.append(f"表示中のチャットに『{expect_text}』があるので切り替えない")
        elif chat_arg:
            # 一覧を開き、名前の合うチャットをちょうど1つ選んで切り替える
            if s["open_list"] > 1:
                result["reason"] = f"一覧を開くボタンが {s['open_list']} 個ある"
                return 1, result
            if s["open_list"] == 1:
                muse.invoke(refs["open_list"][0])
                steps.append("一覧を開いた")
                leftovers.append("開いた一覧")

            def _found(_s, r):
                names = [n for n, _el in (r.get("buttons") or [])]
                return len(match_chat_buttons(names, chat_arg)) >= 1

            s, refs, ok = _poll_scan(muse, _found, OPEN_LIST_TIMEOUT)
            matched = [(n, el) for n, el in (refs.get("buttons") or [])
                       if n in match_chat_buttons([n], chat_arg)]
            if not ok:
                result["reason"] = f"チャット『{chat_arg}』が一覧に見つからない" + note_suffix()
                return 1, result
            if len(matched) != 1:
                result["reason"] = (f"チャット『{chat_arg}』に合うボタンが "
                                     f"{len(matched)} 個ある") + note_suffix()
                return 1, result
            muse.invoke(matched[0][1])
            steps.append(f"チャット『{chat_arg}』を開いた")

            def _switched(s2, r2):
                # 実機(2026-11-05)では、一覧からチャットを開いても一覧は開いたままで、
                # チャット名の取り出し元(…を開くボタン)が消えて chat_title は None になる。
                # そのときは --expect-text の一致だけを切り替わりの証拠にする
                # (expect_text が無ければ証拠が無いので止まる側に倒す)。
                if s2["chat_title"] is None:
                    return bool(expect_text) and any(
                        expect_text in t for t in (r2.get("texts") or []))
                if s2["chat_title"] != chat_arg:
                    return False
                if expect_text and not any(expect_text in t for t in (r2.get("texts") or [])):
                    return False
                return True

            s, refs, ok = _poll_scan(muse, _switched, CHAT_SWITCH_TIMEOUT)
            if not ok:
                if s["chat_title"] != chat_arg:
                    result["reason"] = (f"チャット『{chat_arg}』に切り替わらなかった"
                                         f"(表示中: {s['chat_title']})") + note_suffix()
                else:
                    result["reason"] = (f"切り替え後の画面に『{expect_text}』が見つからない"
                                         "(別のチャットの可能性があるので投稿しない)") + note_suffix()
                return 1, result

        if not to_current:
            if s["new_chat"] == 0:
                muse.invoke(refs["open_list"][0])
                steps.append("一覧を開いた")
                leftovers.append("開いた一覧")
                s, refs, ok = muse.poll(lambda x: x["new_chat"] >= 1, OPEN_LIST_TIMEOUT)
                if not ok:
                    result["reason"] = "一覧を開いたが『新しいサイドチャット』が出ない" + note_suffix()
                    return 1, result
                if s["new_chat"] != 1:
                    result["reason"] = f"『新しいサイドチャット』が {s['new_chat']} 個ある" + note_suffix()
                    return 1, result
            # 「新しいサイドチャット」を押す直前の画面を覚え、切り替わりを待つ(#1)
            sig_before_new = view_signature(s)
            muse.invoke(refs["new_chat"][0])
            steps.append("新しいサイドチャットを作った")
            leftovers.append("作った空のサイドチャット")
            s, refs, ok = muse.poll(
                lambda x: check_new_chat_ready(x, sig_before_new) is None, NEW_CHAT_TIMEOUT)
            if not ok:
                result["reason"] = (check_new_chat_ready(s, sig_before_new)
                                    or "新しいチャットの準備を確かめられない") + note_suffix()
                return 1, result

        title_before = s["chat_title"]
        reason = check_before_set(s)
        if reason:
            result["reason"] = reason + note_suffix()
            return 1, result

        # SetValue の直前に、同じ要素を読み直して空を確かめる(#2)。
        # scan から SetValue までの間に人が打ち始めたら上書きになるため。
        if not is_blank(muse.read_value(refs["input"][0])):
            result["reason"] = ("SetValue の直前に投稿欄が空でなくなった"
                                "(入力中かもしれないので上書きしない)") + note_suffix()
            return 1, result

        muse.set_value(refs["input"][0], message)
        wrote = True
        steps.append("投稿欄に書いた")
        s, refs, ok = muse.poll(lambda x: check_before_invoke(x, message) is None,
                                SEND_APPEAR_TIMEOUT)
        if not ok:
            result["reason"] = (check_before_invoke(s, message)
                                or "送信の準備を確かめられない") + note_suffix()
            if _restore_input(muse, refs, message):
                steps.append("送らずに投稿欄を空に戻した")
            return 1, result

        # 送信 Invoke の直前にも、同じ要素を読み直して一致を確かめる(#2)
        if not _same_text(muse.read_value(refs["input"][0]), message):
            result["reason"] = ("送信の直前に投稿欄の中身が送るはずの文字列と"
                                "一致しなくなった") + note_suffix()
            if _restore_input(muse, refs, message):
                steps.append("送らずに投稿欄を空に戻した")
            return 1, result

        send_attempted = True
        try:
            muse.invoke(refs["send"][0])
        except Exception as e:  # noqa: BLE001  押せたかどうか自体が分からない
            result["reason"] = (f"送信ボタンを押す際に例外: {type(e).__name__}: {e}"
                                "(送れたか不明。二重投稿に注意)") + note_suffix()
            return 1, result
        result["posted_at"] = now_iso()
        steps.append("送信を押した")

        s, refs, ok = muse.poll(lambda x: x["inputs"] == 1 and x["input_empty"] is True,
                                CLEAR_TIMEOUT)
        if not ok:
            result["reason"] = ("送信を押したが投稿欄が空に戻らない"
                                "(送れたか不明。二重投稿に注意)") + note_suffix()
            result["chat_title"] = s["chat_title"]
            return 1, result

        placeholders = _names(muse.profile, "placeholder_titles")
        s, refs, settled = muse.poll(
            lambda x: title_settled(title_before, x["chat_title"], not to_current, placeholders,
                                    message),
            TITLE_TIMEOUT)
        result["chat_title"] = s["chat_title"]
        result["chat_title_settled"] = settled
        result["ok"] = True
        return 0, result
    except Exception as e:  # noqa: BLE001  ここまでに何を押したか(steps)を残して返す
        reason = f"例外: {type(e).__name__}: {e}"
        if send_attempted:
            reason += "(送信を押した後なので送れたか不明。二重投稿に注意)"
        elif wrote:
            # 書いたが送っていない。自分の文面だけを消して戻す(#5)
            if _restore_input(muse, refs, message):
                steps.append("例外のため送らずに投稿欄を空に戻した")
            else:
                steps.append("例外のため投稿欄を空に戻そうとしたが戻せなかった")
                reason += "(投稿欄に書いた文字が残っているかもしれない)"
        result["reason"] = reason + note_suffix()
        return 1, result
    finally:
        muse.close()


# =========================================================================
# 承認カード(approve / watch)
# =========================================================================
def _expect_from_args(args):
    return {"owner": args.owner, "repo": args.repo, "branch": args.branch,
            "allow_paths": list(args.allow_path or [])}


def _check_approval(muse, expect, dry_run=False):
    """今見えている承認カードを1回見て、必要なら押す。

    戻り値は {"status": "none"|"approved"|"would_approve"|"refused", ...}。
    none のときは banner(承認が必要です が出ているか)も入れる。"""
    s, refs = muse.scan()
    card_els = refs.get("cards") or []
    if not card_els:
        return {"status": "none", "banner": bool(s["approval_banner"])}
    if len(card_els) > 1:
        return {"status": "refused", "reason": f"承認カードが {len(card_els)} 枚ある"}
    # カードの中身は、カードの Group 要素の子孫だけから取る。窓全体の並びから
    # 「カードの Group 以降」を切り出す方式(split_approval_cards)だと、カードの後ろに
    # 並ぶ別ペインの要素(実機では 'Chat — Muse' の文書)まで混ざり、未知扱いで止まっていた。
    card_items, card_buttons = muse.card_contents(card_els[0])
    card = parse_approval_card(card_items)
    reason = approval_decision(card, expect)
    if reason:
        return {"status": "refused", "reason": reason, "card": card}
    if dry_run:
        return {"status": "would_approve", "card": card}
    buttons = [el for name, el in card_buttons if name == APPROVE_BUTTON]
    if len(buttons) != 1:
        return {"status": "refused", "card": card,
                "reason": f"『許可する』ボタンが {len(buttons)} 個ある"}
    muse.invoke(buttons[0])
    _s2, _refs2, ok = muse.poll(lambda x: x["approval_cards"] == 0, APPROVE_TIMEOUT)
    if not ok:
        return {"status": "refused", "card": card,
                "reason": "『許可する』を押したがカードが消えない(押せたか不明)"}
    return {"status": "approved", "card": card}


def cmd_approve(args, muse_factory=None):
    expect = _expect_from_args(args)
    if not expect["branch"].startswith("muse/"):
        return 2, {"ok": False, "reason": "--branch は muse/ で始まらなければならない"}
    if not expect["allow_paths"]:
        return 2, {"ok": False, "reason": "--allow-path が1つ以上必要"}
    try:
        muse = (muse_factory or Muse)()
    except MuseNotFound as e:
        return 1, {"ok": False, "reason": str(e)}
    try:
        if not muse.wait_awake():
            pass  # 木が起きていなければ下の scan で「見つからない」になる
        chk = _check_approval(muse, expect, dry_run=bool(args.dry_run))
        if chk["status"] == "none":
            reason = "承認カードが見つからない"
            if chk.get("banner"):
                reason = ("『Muse、承認が必要です』が出ているが、承認カードが"
                          "表示中のチャットに見えない")
            return 1, {"ok": False, "reason": reason}
        if chk["status"] == "refused":
            out = {"ok": False, "reason": chk["reason"]}
            if chk.get("card"):
                out["card"] = public_card(chk["card"])
            return 1, out
        if chk["status"] == "would_approve":
            return 0, {"ok": True, "dry_run": True, "would_approve": True,
                       "card": public_card(chk["card"])}
        return 0, {"ok": True, "approved": True, "approved_at": now_iso(),
                   "card": public_card(chk["card"])}
    finally:
        muse.close()


def _branch_result_sha(repo_dir, branch, path):
    """結果ファイルがブランチに載っていれば、その先端の sha。無ければ None。

    watch のループから毎回呼ぶ軽い確認。失敗はすべて「まだ無い」として扱う
    (理由の切り分けは wait 側の仕事)。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", repo_dir, "ls-remote", "--heads", "origin", branch],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT, env=env)
        if proc.returncode != 0 or not parse_ls_remote(proc.stdout, branch):
            return None
        fetched = subprocess.run(
            ["git", "-C", repo_dir, "fetch", "-q", "origin",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT, env=env)
        if fetched.returncode != 0:
            return None
        head = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", f"refs/remotes/origin/{branch}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT, env=env)
        sha = (head.stdout or "").strip()
        if head.returncode != 0 or not sha:
            return None
        has = subprocess.run(["git", "-C", repo_dir, "cat-file", "-e", f"{sha}:{path}"],
                             capture_output=True, timeout=GIT_TIMEOUT, env=env)
        return sha if has.returncode == 0 else None
    except Exception:  # noqa: BLE001  ループを止めない
        return None


def cmd_watch(args, muse_factory=None):
    """結果ファイルの到着と承認カードを、スクリプトのループで見張る。

    Claude はこのコマンドを1回呼んで結果だけ読めばよい(トークンを食わない)。"""
    expect = _expect_from_args(args)
    if not expect["branch"].startswith("muse/"):
        return 2, {"ok": False, "status": "error",
                   "reason": "--branch は muse/ で始まらなければならない"}
    if not expect["allow_paths"]:
        return 2, {"ok": False, "status": "error", "reason": "--allow-path が1つ以上必要"}
    if not os.path.isdir(args.repo_dir):
        return 2, {"ok": False, "status": "error", "reason": f"リポジトリが無い: {args.repo_dir}"}
    if args.interval <= 0 or args.timeout < 0:
        return 2, {"ok": False, "status": "error", "reason": "--interval は正、--timeout は0以上"}
    # Muse の窓は毎周つかみ直せるようにする。投稿の直後は窓が一瞬見つからない
    # ことがあり(2026-11-05 に2回、最初の1回で諦めて承認を監視しないまま待ち続け、
    # 承認カードが放置された)、窓が作り直されると古い参照は使えなくなるため。
    factory = muse_factory or Muse
    muse = None
    muse_note = None

    def _acquire():
        nonlocal muse, muse_note
        if muse is not None:
            return
        try:
            muse = factory()
            muse_note = None
        except MuseNotFound as e:
            muse = None
            muse_note = f"Muse アプリが見えないあいだは承認を監視できない: {e}"

    approved = []
    start = time.time()
    try:
        while True:
            _acquire()
            sha = _branch_result_sha(args.repo_dir, args.branch, args.path)
            if sha:
                out = {"ok": True, "status": "done", "sha": sha, "branch": args.branch,
                       "path": args.path, "approved": approved,
                       "waited_sec": round(time.time() - start, 1)}
                if muse_note:
                    out["note"] = muse_note
                return 0, out
            if muse is not None:
                try:
                    chk = _check_approval(muse, expect)
                except Exception as e:  # noqa: BLE001  画面の切り替わり中などは次周へ
                    chk = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
                    # 窓が作り直されたかもしれないので、次周でつかみ直す
                    try:
                        muse.close()
                    except Exception:  # noqa: BLE001
                        pass
                    muse = None
                if chk["status"] == "approved":
                    approved.append({"at": now_iso(),
                                     "files": chk["card"]["files"]})
                    continue  # 押したら結果の確認をすぐやり直す
                if chk["status"] == "refused":
                    return 1, {"ok": False, "status": "needs_human",
                               "reason": chk["reason"], "approved": approved,
                               "waited_sec": round(time.time() - start, 1)}
                if chk["status"] == "none" and chk.get("banner"):
                    return 1, {"ok": False, "status": "needs_human",
                               "reason": ("『Muse、承認が必要です』が出ているが、承認カードが"
                                          "表示中のチャットに見えない(別のチャットにある可能性)"),
                               "approved": approved,
                               "waited_sec": round(time.time() - start, 1)}
            elapsed = time.time() - start
            if elapsed >= args.timeout:
                out = {"ok": False, "status": "timeout", "approved": approved,
                       "waited_sec": round(elapsed, 1), "branch": args.branch}
                if muse_note:
                    out["note"] = muse_note
                return 1, out
            time.sleep(min(args.interval, max(0.0, args.timeout - elapsed)))
    finally:
        if muse is not None:
            muse.close()


def _git_timeout(start, total_timeout, first=False):
    """この1回の git 呼び出しに使える秒数(#8)。

    全体の残り時間と GIT_TIMEOUT の小さい方。残りが無いのに最初の1回だけは
    GIT_TIMEOUT で試す(--timeout 0 でも1回は確かめる、従来の挙動)。"""
    remaining = total_timeout - (time.time() - start)
    if remaining <= 0:
        return GIT_TIMEOUT if first else None
    return min(GIT_TIMEOUT, remaining)


def cmd_wait(args):
    """ブランチが remote に現れ、--path のファイルが載るまで待つ。UIA は使わない。

    --path 無しでは完了を判定できない(ブランチは作業前に作られることがある)ので、
    ブランチが現れただけなら ok:false / appeared:true で返す(#7)。"""
    if not os.path.isdir(args.repo):
        return 2, {"ok": False, "reason": f"リポジトリが無い: {args.repo}"}
    if args.interval <= 0 or args.timeout < 0:
        return 2, {"ok": False, "reason": "--interval は正、--timeout は0以上"}
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # 認証を対話で聞かれて固まらないように
    start = time.time()
    last_error = None

    def timeout_result():
        out = {"ok": False, "reason": "timeout",
               "waited_sec": round(time.time() - start, 1), "branch": args.branch}
        if last_error:
            out["last_error"] = last_error
        return 1, out

    def run_git(argv, first=False):
        """全体の制限時間を守って git を1回呼ぶ。時間が尽きていれば
        TimeoutExpired を投げて、呼び出し側の timeout 処理に寄せる。"""
        budget = _git_timeout(start, args.timeout, first=first)
        if budget is None:
            raise subprocess.TimeoutExpired(argv, 0)
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=budget, env=env)

    first_call = True
    while True:
        try:
            proc = run_git(["git", "-C", args.repo, "ls-remote", "--heads",
                            "origin", args.branch], first=first_call)
            first_call = False
            if proc.returncode == 0:
                sha = parse_ls_remote(proc.stdout, args.branch)
                last_error = None
                if sha and not args.path:
                    # ブランチが現れただけでは終わっていない。Muse はブランチを
                    # 先に作り、結果は後から push する。完了は --path でしか言えない。
                    return 1, {"ok": False, "appeared": True,
                               "reason": "ブランチは現れたが完了は未確認"
                                         "(--path を付けてください)",
                               "appeared_after_sec": round(time.time() - start, 1),
                               "sha": sha, "branch": args.branch}
                if sha and args.path:
                    fetched = run_git(
                        ["git", "-C", args.repo, "fetch", "-q", "origin",
                         f"+refs/heads/{args.branch}:refs/remotes/origin/{args.branch}"])
                    if fetched.returncode != 0:
                        last_error = (f"git fetch の終了コード {fetched.returncode}: "
                                      f"{redact(fetched.stderr)}")
                        sha = None
                    else:
                        # 成功として返す sha は fetch 後の先端で取り直す(#9)。
                        # ls-remote の sha と、その間に push された先端がずれうるため。
                        head = run_git(["git", "-C", args.repo, "rev-parse",
                                        f"refs/remotes/origin/{args.branch}"])
                        fetched_sha = (head.stdout or "").strip()
                        if head.returncode != 0 or not fetched_sha:
                            last_error = (f"git rev-parse の終了コード "
                                          f"{head.returncode}: {redact(head.stderr)}")
                            sha = None
                        else:
                            has = run_git(["git", "-C", args.repo, "cat-file", "-e",
                                           f"{fetched_sha}:{args.path}"])
                            if has.returncode != 0:
                                sha = None  # ブランチはあるが結果ファイルがまだ
                            else:
                                return 0, {"ok": True,
                                           "appeared_after_sec": round(time.time() - start, 1),
                                           "sha": fetched_sha, "branch": args.branch,
                                           "path": args.path}
            else:
                last_error = f"git の終了コード {proc.returncode}: {redact(proc.stderr)}"
        except subprocess.TimeoutExpired:
            last_error = "git が制限時間内に返らない"
        elapsed = time.time() - start
        if elapsed >= args.timeout:
            return timeout_result()
        time.sleep(min(args.interval, max(0.0, args.timeout - elapsed)))


# =========================================================================
# 入口
# =========================================================================
class _JsonArgParser(argparse.ArgumentParser):
    """引数の誤りも JSON 1つで返す(呼ぶ側が標準出力だけを読めば済むように)。"""

    def error(self, message):
        _emit({"ok": False, "reason": f"引数の誤り: {message}"})
        sys.exit(2)


# =========================================================================
# 窓口チャット(--desk)
# =========================================================================
# 【なぜ窓口チャットなのか】
# 依頼のたびに新しいサイドチャットを作ると、チャットが増えてアーカイブの手間も
# 増える(陽太さんの要望で 20261105 に変更)。依頼の中身はすべて GitHub の依頼書に
# あり、Muse は毎回それを読むので、会話の流れに頼る必要はない。そこでリポジトリ
# ごとに「窓口」のチャットを1つ決め、毎回そこに投稿する。
#
# 窓口のチャット名と、最後に投稿した依頼 id を DESK_FILE に控える。次の投稿では
# その名前のチャットを開き、前回の id が画面にあることを確かめてから書く
# (--chat --expect-text と同じ確かめ方。一覧が開いたままだとチャット名を
# 読めないので、id が唯一の証拠になる)。
# チャット名は個人の作業内容なので、public リポジトリには置かない。
#
# 窓口が控えに無い(初回)か --new-desk のときは、新しいサイドチャットを作り、
# Muse が付けた名前を新しい窓口として控える。控えにあるのに一覧で見つからない
# ときは、勝手に作り直さず止まる(陽太さんがアーカイブした・名前が変わった、
# などを人が見て --new-desk を付け直す)。
DESK_FILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "traytools", "muse_desks.json")


def load_desks(path=None):
    path = path or DESK_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_desks(desks, path=None):
    path = path or DESK_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(desks, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def desk_plan(desks, name, new_desk=False):
    """窓口への投稿の仕方を決める。(mode, chat, expect_text) を返す。

    mode: "new"  = 新しいサイドチャットを作る(控えが無い・--new-desk)
          "chat" = 名前の分かっている窓口チャット(表示中なら切り替えない)
          "here" = 名前は未登録だが前回の id は分かっている。表示中のチャットに
                   その id が見えるときだけ投稿する"""
    entry = desks.get(name) if isinstance(desks, dict) else None
    if new_desk or not isinstance(entry, dict):
        return "new", None, None
    last_id = entry.get("last_id") or None
    if entry.get("chat"):
        return "chat", entry["chat"], last_id
    if last_id:
        return "here", None, last_id
    return "new", None, None


def desk_record(desks, name, chat, request_id, when, new_chat):
    """投稿できたあとの控えを作る(元の dict は変えない)。"""
    out = dict(desks) if isinstance(desks, dict) else {}
    old = out.get(name) if isinstance(out.get(name), dict) else {}
    entry = {"chat": chat, "last_id": request_id, "updated": when}
    entry["since"] = when if new_chat else old.get("since", when)
    entry["posts"] = 1 if new_chat else int(old.get("posts", 0) or 0) + 1
    out[name] = entry
    return out


def cmd_post_desk(args, message, muse_factory=None, desk_path=None):
    """--desk: 窓口チャットを引いて cmd_post に渡し、投稿できたら控えを更新する。"""
    desks = load_desks(desk_path)
    mode, chat, expect = desk_plan(desks, args.desk, args.new_desk)
    args.chat = chat
    args.expect_text = expect
    args.current_chat = mode == "here"
    code, out = cmd_post(args, message, muse_factory=muse_factory)
    out["desk"] = args.desk
    out["desk_mode"] = mode
    finished = code == 0 and not args.dry_run
    target = {"new": "新しい窓口チャット", "chat": f"窓口チャット『{chat}』",
              "here": "表示中の窓口チャット"}[mode]
    if mode == "new":
        verb = "を作った" if finished else "を作る(予定)"
    else:
        verb = "に投稿した" if finished else "に投稿する(予定)"
    out["desk_action"] = target + verb
    if code != 0:
        if mode == "chat" and "見つからない" in (out.get("reason") or ""):
            out["reason"] += ("(窓口チャットがアーカイブされたか名前が変わったかもしれない。"
                              "確かめてから --new-desk で作り直す)")
        if mode == "here":
            out["reason"] += ("(窓口チャットの名前が未登録。Muse でそのチャットを表示すれば"
                              "続けられる。名前が分かれば desk --set で控える)")
        return code, out
    if args.dry_run:
        return code, out
    title = chat
    if title is None and out.get("chat_title"):
        # 新しいチャットでは名前が定まったときだけ、表示中への投稿では読めれば採る
        if mode == "here" or out.get("chat_title_settled"):
            title = out["chat_title"]
    if title is None:
        out["desk_note"] = ("窓口チャットの名前は読めなかった。前回の id だけ控えたので、"
                            "次回は表示中のチャットにその id があれば続けて投稿する")
    try:
        save_desks(desk_record(desks, args.desk, title, args.request_id, now_iso(),
                               mode == "new"), desk_path)
        out["desk_chat"] = title
    except OSError as e:
        out["desk_note"] = f"控え(muse_desks.json)を書けなかった: {e}"
    return code, out


def cmd_desk(args, desk_path=None):
    """窓口の控えを見る / 手で直す(名前が定まらなかったとき、名前が変わったとき)。"""
    desks = load_desks(desk_path)
    if not args.set:
        return 0, {"ok": True, "desks": desks, "path": desk_path or DESK_FILE}
    if not args.chat:
        return 2, {"ok": False, "reason": "引数の誤り: --set には --chat が要る"}
    old = desks.get(args.set) if isinstance(desks.get(args.set), dict) else {}
    when = now_iso()
    desks = dict(desks)
    desks[args.set] = {"chat": args.chat,
                       "last_id": args.last_id or old.get("last_id"),
                       "updated": when, "since": old.get("since", when),
                       "posts": int(old.get("posts", 0) or 0)}
    save_desks(desks, desk_path)
    return 0, {"ok": True, "desk": args.set, "entry": desks[args.set]}


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
    post.add_argument("--chat", help="この名前の既存チャットを開いて投稿する")
    post.add_argument("--expect-text",
                      help="--chat で切り替えた画面にこの文字列があることを確かめる")
    post.add_argument("--desk", help="このリポジトリの窓口チャットに投稿する(例: tray-tools)")
    post.add_argument("--request-id", help="--desk で、今回の依頼 id(次回の確かめに使う)")
    post.add_argument("--new-desk", action="store_true",
                      help="--desk で、新しいサイドチャットを作って窓口を切り替える")
    post.add_argument("--dry-run", action="store_true", help="UI を一切変えずに計画だけ返す")
    post.add_argument("--allow-multiline", action="store_true",
                      help="改行入りの投稿文を許す(送信の挙動は未検証)")
    desk = sub.add_parser("desk", help="窓口チャットの控えを見る / 直す")
    desk.add_argument("--set", help="この窓口(例: tray-tools)の控えを書き換える")
    desk.add_argument("--chat", help="--set で、窓口にするチャット名")
    desk.add_argument("--last-id", help="--set で、そのチャットに最後に投稿した依頼 id")
    approve = sub.add_parser("approve", help="承認カードが指定と一致するときだけ許可する")
    approve.add_argument("--owner", required=True)
    approve.add_argument("--repo", required=True)
    approve.add_argument("--branch", required=True)
    approve.add_argument("--allow-path", action="append", required=True,
                         help="許可してよいファイルパス(複数指定可)")
    approve.add_argument("--dry-run", action="store_true", help="押さずに判定だけ返す")
    watch = sub.add_parser("watch", help="結果ファイルの到着と承認カードを見張る")
    watch.add_argument("--repo-dir", required=True)
    watch.add_argument("--owner", required=True)
    watch.add_argument("--repo", required=True)
    watch.add_argument("--branch", required=True)
    watch.add_argument("--path", required=True, help="結果ファイルのパス")
    watch.add_argument("--allow-path", action="append", required=True,
                       help="許可してよいファイルパス(複数指定可)")
    watch.add_argument("--timeout", type=float, default=3600)
    watch.add_argument("--interval", type=float, default=15)
    wait = sub.add_parser("wait", help="remote にブランチと結果ファイルが現れるまで待つ")
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
        _emit({"ok": False, "reason": "サブコマンド(status / post / desk / approve / watch / wait)を指定する"})
        return 2
    try:
        if args.cmd == "status":
            code, out = cmd_status(args)
        elif args.cmd == "post":
            if args.chat and args.current_chat:
                _emit({"ok": False, "reason": "引数の誤り: --chat と --current-chat は同時に使えない"})
                return 2
            if args.expect_text and not args.chat:
                _emit({"ok": False, "reason": "引数の誤り: --expect-text は --chat と一緒に使う"})
                return 2
            if args.desk and (args.chat or args.current_chat or args.expect_text):
                _emit({"ok": False, "reason": "引数の誤り: --desk は --chat / --current-chat / "
                                              "--expect-text と同時に使えない"})
                return 2
            if args.desk and not args.request_id:
                _emit({"ok": False, "reason": "引数の誤り: --desk には --request-id が要る"})
                return 2
            if (args.new_desk or args.request_id) and not args.desk:
                _emit({"ok": False, "reason": "引数の誤り: --new-desk / --request-id は --desk と一緒に使う"})
                return 2
            try:
                message = read_message(args)
            except OSError as e:
                _emit({"ok": False, "reason": f"--message-file を読めない: {e}"})
                return 2
            err = message_error(message, args.allow_multiline)
            if err:
                _emit({"ok": False, "reason": err})
                return 2
            if args.desk:
                code, out = cmd_post_desk(args, message)
            else:
                code, out = cmd_post(args, message)
        elif args.cmd == "desk":
            code, out = cmd_desk(args)
        elif args.cmd == "approve":
            code, out = cmd_approve(args)
        elif args.cmd == "watch":
            code, out = cmd_watch(args)
        else:
            code, out = cmd_wait(args)
    except Exception as e:  # noqa: BLE001  COMError は OSError でないので広く受ける
        code, out = 1, {"ok": False, "reason": f"例外: {type(e).__name__}: {e}"}
    _emit(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
