# -*- coding: utf-8 -*-
"""AI対話モード: 配達の段階制御（純ロジック）と、ワーカーとのやりとりの約束事。

Win32 / COM / Qt を import しない。窓や UIA に触る操作はすべて `ops` オブジェクトへ注入する
（本物は dialogue_worker.RealOps、テストではフェイク）。ここは「どの順に試し、いつ止めるか」だけを持つ。

配達の順（deliver）:
  1. 宛先の窓を探す（見つからない・曖昧なら止める。別の窓へは送らない）→ 入力欄を探す
  2. UIA で値を設定 → 読み戻し。一致したときだけ delivered（stage=uia）
  3. 不一致なら前面化（UIA SetFocus → 最小化なら復元 → SetForegroundWindow）。**前面を確認できるまで
     キー送信（貼り付け・クリック）をしない**。入力欄のフォーカスを確認 → 貼り付け → 読み戻し（stage=paste）
  4. 前面化できない／フォーカスを確認できないときは、点の下が宛先の窓であることを確認したうえで
     本物のクリック → 前面確認 → 貼り付け → 読み戻し（stage=click_paste）
  5. どれも不可なら undelivered（理由つき）。確定したテキストは呼び出し側が保持する。

ops の約束（すべて辞書か基本型を返す。例外は deliver が受けて undelivered にする）:
  find_window(profile, hwnd) -> {status: ok|none|ambiguous, hwnd, is_iconic, rect, reason}
  wake(hwnd) / find_input(hwnd, profile) -> {found, how, element: {rect, ...}, error, candidates}
  read_value(hwnd, profile) -> {ok, text} / write_value(hwnd, profile, text) -> {ok, reason}
  set_focus(hwnd, profile) -> {ok, focused_matches}
  foreground_root() -> int / wait_foreground(hwnd, timeout) -> bool
  bring_to_front(hwnd, profile, method) -> dict   (method: uia_set_focus | show_restore_then_set | set_foreground)
  root_of_point(x, y) -> int / click(x, y, expected_root) -> {clicked: bool, reason}
  clipboard_snapshot() -> {restorable, ...} / clipboard_set_text(text) -> bool / clipboard_restore(snap) -> bool
  paste(expected_root, mode) -> {sent: bool, reason}   mode: insert | replace | append
      （paste と click は実装の中でも前面・点の下を再確認する。ここでの確認は二重の安全）
  last_input_ms() -> int|None / flash(hwnd)（任意）
  --- 送信まで行うとき（options.send）だけ使う ---
  find_button(hwnd, profile, names, near_rect) -> {found, enabled, name, rect}   (名前は完全一致、near_rect に近いもの)
  invoke_button(hwnd, profile, names, near_rect) -> {invoked, reason}            (UIA Invoke。前面化・フォーカス不要)
  send_enter(expected_root) -> {sent, reason}                                    (送る直前に前面を再確認する)

送信（options.send=True）: **入力欄への配達が読み戻し一致で確認できたときだけ**行い、1回しか試さない（自動で再試行しない）。
  0. wait_while_busy: 「回答中」のボタン（busy_button）が見えている間は待つ（busy_wait_sec まで。超えたら送らない）
  1. 送信ボタン（send_button）を UIA で Invoke
  2. ボタンが見つからない／無効なら、前面確認＋入力欄のフォーカス確認が取れてから Enter キー
  3. 入力欄が空（または元の書きかけだけ）に戻ったかを読み戻して判定。判定できなければ sent="unknown"
  結果に sent: True / False / "unknown"、send_stage（button|enter）、send_reason を足す。
"""
from __future__ import annotations

import json
import time

from dialogue_profile import judge_readback, normalize_text, texts_equivalent

# ---------------------------------------------------------------------------
# ワーカーとのプロトコル（JSON 1行 = 1リクエスト／1レスポンス、UTF-8）
# ---------------------------------------------------------------------------

OPS = ("ping", "pick", "resolve", "deliver", "cleanup", "shutdown")


def encode_line(obj):
    """辞書を JSON 1行（末尾 LF）の UTF-8 バイト列にする。"""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def decode_line(raw):
    """バイト列（または文字列）1行を辞書にする。JSON でない・辞書でないなら ValueError。"""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"UTF-8 ではない: {e}") from e
    s = (raw or "").strip()
    if not s:
        raise ValueError("空行")
    try:
        obj = json.loads(s)
    except ValueError as e:
        raise ValueError(f"JSON ではない: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("JSON の最上位が辞書ではない")
    return obj


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

DEFAULT_OPTIONS = {
    "existing_text": "append",          # 書きかけがあるとき: append / overwrite / abort
    "keep_idle_foreground": False,      # True: 直近の入力が quiet_input_sec 以内なら前面化を待つ（待ち切れなければ未配達）
    "quiet_input_sec": 3.0,
    "quiet_wait_sec": 5.0,
    "return_focus": False,              # True: 前面化したとき、終わったら元の前面窓へ戻す
    "allow_click": True,                # False: 本物のクリックを使わない
    "allow_unrestorable_clipboard": False,   # False: クリップボードを元に戻せないなら貼り付けを諦める
    "front_methods": ["uia_set_focus", "show_restore_then_set", "set_foreground"],
    "foreground_timeout_sec": 2.0,
    "readback_tries": 3,
    "readback_interval_sec": 0.2,
    "paste_settle_sec": 0.3,
    "force_on_mismatch": False,         # True: 読み戻しが一致しなくても（文字が入っていれば）配達済みとして、送信まで進める
    # --- 送信まで行う ---
    "send": False,                      # True: 配達（読み戻し一致）のあと、送信まで行う
    "wait_while_busy": True,            # 回答中（busy_button が見える）なら送信を待つ
    "busy_wait_sec": 120.0,
    "busy_poll_sec": 0.5,
    "send_verify_sec": 5.0,             # 送信後に入力欄が空になるのを待つ秒数
    "send_verify_interval_sec": 0.25,
    "send_buttons": ["送信", "メッセージの送信", "メッセージを送信", "Send", "Send message"],
    "busy_buttons": ["生成を停止する", "メッセージの割り込み", "停止", "Stop", "Stop generating", "Stop response"],
}

EXISTING_POLICIES = ("append", "overwrite", "abort")

# 理由コード（reason）
R_EMPTY = "empty_text"
R_NOT_FOUND = "window_not_found"
R_AMBIGUOUS = "window_ambiguous"
R_NO_INPUT = "input_not_found"
R_UNREADABLE = "existing_unreadable"
R_ABORT = "existing_text_abort"
R_DIRTY = "input_dirty"
R_USER_ACTIVE = "user_active"
R_FG_FAILED = "foreground_failed"
R_FG_LOST = "foreground_lost"
R_NO_FOCUS = "focus_not_confirmed"
R_COVERED = "click_point_covered"
R_CLICK_FAILED = "click_failed"
R_CLIP_NOT_RESTORABLE = "clipboard_not_restorable"
R_CLIP_ERROR = "clipboard_error"
R_PASTE_NOT_SENT = "paste_not_sent"
R_MISMATCH = "readback_mismatch"
R_OPS_ERROR = "ops_error"
# 送信の理由コード（send_reason）
R_BUSY_TIMEOUT = "busy_timeout"
R_SEND_FG = "send_foreground_failed"
R_SEND_NO_FOCUS = "send_focus_not_confirmed"
R_SEND_FG_LOST = "send_foreground_lost"
R_SEND_NOT_SENT = "send_not_sent"
R_SEND_UNKNOWN = "send_unconfirmed"


class ForegroundNotConfirmed(Exception):
    """前面を確認できないのでキー送信・クリックをしなかった。"""


def merge_options(options):
    o = dict(DEFAULT_OPTIONS)
    for k, v in (options or {}).items():
        if k in DEFAULT_OPTIONS and v is not None:
            o[k] = v
    if o["existing_text"] not in EXISTING_POLICIES:
        o["existing_text"] = "append"
    return o


def plan_existing(existing, text, policy):
    """書きかけ（existing）の扱いを決める純関数。
    戻りは {action: write|abort, final: 入力欄に入るべき全文, payload: 貼り付ける文字列, mode: insert|replace|append}。
    existing が None（読めなかった）のときは overwrite だけが通る（replace で上書き）。"""
    if existing is None:
        if policy == "overwrite":
            return {"action": "write", "final": text, "payload": text, "mode": "replace"}
        return {"action": "abort", "final": "", "payload": "", "mode": "insert"}
    if normalize_text(existing) == "":
        return {"action": "write", "final": text, "payload": text, "mode": "insert"}
    if policy == "abort":
        return {"action": "abort", "final": "", "payload": "", "mode": "insert"}
    if policy == "overwrite":
        return {"action": "write", "final": text, "payload": text, "mode": "replace"}
    sep = "" if existing.endswith(("\n", "\r")) else "\n"
    return {"action": "write", "final": existing + sep + text, "payload": sep + text, "mode": "append"}


def click_point(elem_rect, win_rect, profile):
    """クリックする点。入力欄の矩形の中心。矩形が無ければ保存したクリック位置。決められなければ None。"""
    if elem_rect and elem_rect[2] - elem_rect[0] > 0 and elem_rect[3] - elem_rect[1] > 0:
        return ((elem_rect[0] + elem_rect[2]) // 2, (elem_rect[1] + elem_rect[3]) // 2)
    rel = (profile or {}).get("click_rel")
    if rel and win_rect:
        return (win_rect[0] + rel[0], win_rect[1] + rel[1])
    return None


# ---------------------------------------------------------------------------
# 配達
# ---------------------------------------------------------------------------

class _Run:
    """1回の配達の記録（段階ごとの所要・試行の履歴・結果の整形）。"""

    def __init__(self, ops, clock):
        self.ops = ops
        self.clock = clock
        self.t0 = clock()
        self.last = self.t0
        self.timings = {}
        self.calls = {}
        self.attempts = []
        self.root = 0
        self.fg_before = 0
        self.extra = {}
        self.on_done = None     # 結果を返す直前に1回だけ呼ぶ（前面を元へ戻す等）

    def lap(self, name):
        now = self.clock()
        self.timings[name] = self.timings.get(name, 0) + int(round((now - self.last) * 1000))
        self.last = now

    def note(self, stage, **kw):
        self.attempts.append({"stage": stage, **kw})

    def call(self, name, *a):
        """ops のメソッドを呼ぶ。戻りは (結果, 例外メッセージ or "")。COMError 等も受ける。"""
        fn = getattr(self.ops, name, None)
        if fn is None:
            return None, f"ops.{name} が無い"
        t = self.clock()
        try:
            return fn(*a), ""
        except Exception as e:  # noqa: BLE001  COMError は OSError ではない
            return None, f"{type(e).__name__}: {e}"
        finally:
            c = self.calls.setdefault(name, [0, 0])      # 呼び出しごとの回数と合計ミリ秒（どこで時間がかかるかの測定用）
            c[0] += 1
            c[1] += int(round((self.clock() - t) * 1000))

    def done(self, status, stage, reason="", readback=None, detail=""):
        if self.on_done:
            hook, self.on_done = self.on_done, None
            hook()
        self.timings["total"] = int(round((self.clock() - self.t0) * 1000))
        if status != "delivered" and self.root:
            self.call("flash", self.root)
        out = {"status": status, "stage": stage, "reason": reason, "detail": detail,
               "readback": readback, "timings": self.timings, "calls": self.calls, "attempts": self.attempts,
               "window": self.root, "foreground_before": self.fg_before}
        out.update(self.extra)
        return out


def _guard_foreground(ops, root):
    """キー送信の直前の確認。前面が root でなければ例外（何も送らない）。"""
    fg = ops.foreground_root()
    if not root or not fg or int(fg) != int(root):
        raise ForegroundNotConfirmed(f"前面={fg} 想定={root}")


def _empty_set(profile):
    """「空の入力欄」が返す文字（プレースホルダー・入力欄の名前・profile の empty_values）の正規化済みの集合。"""
    vals = list((profile or {}).get("empty_values") or []) + [(profile or {}).get("input_name") or ""]
    return {normalize_text(x) for x in vals if str(x).strip()}


def _read_text(run, hwnd, profile):
    r, err = run.call("read_value", hwnd, profile)
    if err or not r or not r.get("ok"):
        return None
    return r.get("text")


def _readback(run, hwnd, profile, expected, opts, retries=None):
    """読み戻しの判定を最大 retries 回（間隔つき）。戻りは (verdict, actual)。ok になったら即返す。"""
    verdict, actual = "unreadable", None
    n = max(1, int(retries if retries is not None else opts["readback_tries"]))
    for k in range(n):
        actual = _read_text(run, hwnd, profile)
        verdict = judge_readback(expected, actual)
        if verdict == "ok":
            break
        if k < n - 1:
            run.sleep(opts["readback_interval_sec"])
    return verdict, actual


def _button_names(profile, key, default):
    v = (profile or {}).get(key)
    if isinstance(v, str):
        v = [v]
    names = [str(x) for x in (v or []) if str(x).strip()]
    return names or list(default)


def _send_phase(run, root, profile, elem_rect, existing, opts, sleep, fw):
    """入力欄への配達が読み戻し一致で確認できたあとだけ呼ぶ。**1回しか試さない**（二重送信防止）。
    結果は run.extra の sent / send_stage / send_reason に入れる。"""
    ex = run.extra
    ex.update(sent=False, send_stage="", send_reason="")

    def fail(reason, detail=""):
        ex["send_reason"] = reason
        if detail:
            ex["send_detail"] = detail
        run.note("send", ok=False, reason=reason, detail=detail)

    # 0. 回答中なら待つ（busy_button が一度も見つからないアプリでは待たない）
    if opts["wait_while_busy"]:
        names = _button_names(profile, "busy_button", opts["busy_buttons"])
        step = max(0.05, float(opts["busy_poll_sec"]))
        t_busy0 = run.clock()     # 待ち時間は時計で測る（画面の確認 1 回に数秒かかり、回数×間隔では短く数えてしまう）
        waited = 0.0
        while True:
            r, err = run.call("find_button", root, profile, names, elem_rect)   # 入力欄のまわりだけ見る
            waited = run.clock() - t_busy0
            if err or not (r or {}).get("found"):
                break
            if waited >= float(opts["busy_wait_sec"]):
                ex["busy_waited_sec"] = waited
                return fail(R_BUSY_TIMEOUT)
            sleep(step)
            waited = run.clock() - t_busy0
        if waited > step:
            ex["busy_waited_sec"] = waited
    run.lap("busy")

    # 1. 送信ボタンを UIA で Invoke（前面化・フォーカスは要らない）
    names = _button_names(profile, "send_button", opts["send_buttons"])
    fb, err = run.call("find_button", root, profile, names, elem_rect)
    stage = ""
    btn_ok = bool(not err and fb and fb.get("found") and fb.get("enabled", True))
    if btn_ok:
        inv, err2 = run.call("invoke_button", root, profile, names, elem_rect)
        if not err2 and (inv or {}).get("invoked"):
            stage = "button"
        run.note("send_button", invoked=bool(stage), error=err2 or (inv or {}).get("reason", ""))
    else:
        run.note("send_button", found=bool(fb and fb.get("found")), enabled=(fb or {}).get("enabled"), error=err)

    # 2. ボタンが使えなければ Enter。前面確認とフォーカス確認が取れてから
    if not stage:
        cur, _e = run.call("foreground_root")
        if int(cur or 0) != root:
            methods = list(opts["front_methods"])
            if (fw or {}).get("is_iconic") and "show_restore_then_set" in methods:
                methods.remove("show_restore_then_set")
                methods.insert(0, "show_restore_then_set")
            got = False
            for m in methods:
                _r, e1 = run.call("bring_to_front", root, profile, m)
                ok, e2 = run.call("wait_foreground", root, opts["foreground_timeout_sec"])
                run.note("send_front", method=m, ok=bool(ok) and not e2, error=e1 or e2)
                if ok and not e2:
                    got = True
                    break
            if not got:
                return fail(R_SEND_FG)
        foc, e3 = run.call("set_focus", root, profile)
        fg2, _e = run.call("foreground_root")
        if e3 or not (foc or {}).get("focused_matches") or int(fg2 or 0) != root:
            return fail(R_SEND_NO_FOCUS, e3)
        try:
            _guard_foreground(run.ops, root)
        except ForegroundNotConfirmed as e:
            return fail(R_SEND_FG_LOST, str(e))
        except Exception as e:  # noqa: BLE001
            return fail(R_OPS_ERROR, f"{type(e).__name__}: {e}")
        er, e4 = run.call("send_enter", root)
        if e4 or not (er or {}).get("sent"):
            return fail(R_SEND_NOT_SENT, e4 or (er or {}).get("reason", ""))
        stage = "enter"
    ex["send_stage"] = stage
    run.lap("send")

    # 3. 送れたか: 入力欄が空（または元の書きかけだけ）に戻ったことを読み戻して判定する
    n = max(1, int(round(float(opts["send_verify_sec"]) / max(0.05, float(opts["send_verify_interval_sec"])))))
    state = "unreadable"
    for _k in range(n):
        sleep(opts["send_verify_interval_sec"])
        actual = _read_text(run, root, profile)
        if actual is None:
            state = "unreadable"
            continue
        if (normalize_text(actual) == "" or texts_equivalent(actual, existing or "")
                or normalize_text(actual) in _empty_set(profile)):     # 送信後の空の入力欄が、プレースホルダーを返すアプリ
            ex["sent"] = True
            run.note("send", ok=True, via=stage)
            return
        state = "remain"
    ex["sent"] = "unknown"
    ex["send_reason"] = R_SEND_UNKNOWN
    run.note("send", ok="unknown", via=stage, readback=state)


def _after_input_ok(run, root, profile, elem_rect, existing, opts, sleep, fw):
    if opts["send"]:
        try:
            _send_phase(run, root, profile, elem_rect, existing, opts, sleep, fw)
        except Exception as e:  # noqa: BLE001  入力は済んでいる。送信まわりの例外では配達を未配達にしない
            run.extra.update(sent="unknown" if run.extra.get("send_stage") else False,
                             send_reason=R_OPS_ERROR, send_detail=f"{type(e).__name__}: {e}")


def deliver(ops, profile, text, hwnd=0, options=None, clock=time.monotonic, sleep=time.sleep):
    """テキストを宛先の入力欄へ入れる（options.send=True のときは、読み戻し一致のあと送信まで）。戻りは
    {status: delivered|undelivered, stage, reason, detail, readback, timings, attempts, window, sent?, send_stage?, ...}。"""
    opts = merge_options(options)
    run = _Run(ops, clock)
    run.sleep = sleep

    if not normalize_text(text):
        return run.done("undelivered", "start", R_EMPTY)

    # --- 1. 窓と入力欄 ---
    fw, err = run.call("find_window", profile, hwnd)
    run.lap("window")
    if err:
        return run.done("undelivered", "window", R_OPS_ERROR, detail=err)
    st = (fw or {}).get("status")
    if st == "ambiguous":
        return run.done("undelivered", "window", R_AMBIGUOUS, detail=str(fw.get("candidates", ""))[:200])
    root = int((fw or {}).get("hwnd") or 0)
    if st != "ok" or not root:
        return run.done("undelivered", "window", R_NOT_FOUND)
    run.root = root
    win_rect = fw.get("rect")
    fg, _e = run.call("foreground_root")
    run.fg_before = int(fg or 0)

    run.call("wake", root)
    fi, err = run.call("find_input", root, profile)
    run.lap("find_input")
    if err:
        return run.done("undelivered", "find_input", R_OPS_ERROR, detail=err)
    if not (fi or {}).get("found"):
        return run.done("undelivered", "find_input", R_NO_INPUT, detail=str((fi or {}).get("error", ""))[:200])
    elem_rect = ((fi.get("element") or {}).get("rect")) or None
    run.extra["input_how"] = fi.get("how", "")

    # --- 書きかけの扱い ---
    existing = _read_text(run, root, profile)
    # 入力欄が空のとき、プレースホルダー（「一行メッセージ」など）を値として返すアプリがある。それを書きかけと誤解して
    # 本文の前に入れてしまわないよう、プロファイルの empty_values に載っている文字列は「空」として扱う
    if existing is not None:
        empties = _empty_set(profile)
        if empties and normalize_text(existing) in empties:
            existing = ""
    plan = plan_existing(existing, text, opts["existing_text"])
    if plan["action"] == "abort":
        reason = R_UNREADABLE if existing is None else R_ABORT
        return run.done("undelivered", "existing", reason)
    final, payload, mode = plan["final"], plan["payload"], plan["mode"]
    run.extra["existing_mode"] = mode

    # --- 2. UIA で値を設定 → 読み戻し ---
    w, err = run.call("write_value", root, profile, final)
    w = w or {}
    wrote = bool(w.get("ok")) and not err
    verdict = "not_written"
    if wrote:
        verdict, _a = _readback(run, root, profile, final, opts, retries=2)
    run.note("uia", write_ok=wrote, reason=w.get("reason", "") or err, readback=verdict)
    run.lap("uia")
    if wrote and verdict == "ok":
        _after_input_ok(run, root, profile, elem_rect, existing, opts, sleep, fw)
        return run.done("delivered", "uia", readback="ok")
    if wrote and verdict == "mismatch" and opts["force_on_mismatch"]:
        run.note("force_on_mismatch", verdict=verdict, via="uia")
        _after_input_ok(run, root, profile, elem_rect, existing, opts, sleep, fw)
        return run.done("delivered", "uia", readback="mismatch_forced")

    # 紛れ込んだ内容を、貼り付けの前に元へ戻す（二重に入れないため）。replace は全選択で上書きするので不要
    if mode != "replace" and wrote and verdict not in ("ok",):
        cur = _read_text(run, root, profile)
        if cur is not None and not texts_equivalent(cur, existing or ""):
            run.call("write_value", root, profile, existing or "")
            cur = _read_text(run, root, profile)
            run.note("restore_original", readback=judge_readback(existing or "", cur) if cur is not None else "unreadable")
            if cur is None or not (texts_equivalent(cur, existing or "")):
                return run.done("undelivered", "uia", R_DIRTY, readback=verdict)

    # --- 3. 前面化 ---
    cur_fg, _e = run.call("foreground_root")
    need_front = int(cur_fg or 0) != root
    if need_front and opts["keep_idle_foreground"]:
        waited = 0.0
        step = 0.25
        while True:
            ms, _e = run.call("last_input_ms")
            if ms is None or ms >= float(opts["quiet_input_sec"]) * 1000:
                break
            if waited >= float(opts["quiet_wait_sec"]):
                run.lap("front")
                return run.done("undelivered", "front", R_USER_ACTIVE, readback=verdict)
            sleep(step)
            waited += step
    front_ok = not need_front
    if need_front:
        methods = list(opts["front_methods"])
        if (fw or {}).get("is_iconic") and "show_restore_then_set" in methods:
            methods.remove("show_restore_then_set")
            methods.insert(0, "show_restore_then_set")
        for m in methods:
            _r, err = run.call("bring_to_front", root, profile, m)
            ok, err2 = run.call("wait_foreground", root, opts["foreground_timeout_sec"])
            run.note("front", method=m, ok=bool(ok) and not err2, error=err or err2)
            if ok and not err2:
                front_ok = True
                break
    run.lap("front")
    if need_front and opts["return_focus"]:
        run.on_done = lambda: _return_focus(run, root)

    focus_ok = False
    if front_ok:
        foc, err = run.call("set_focus", root, profile)
        fg2, _e = run.call("foreground_root")
        focus_ok = (not err and bool((foc or {}).get("focused_matches")) and int(fg2 or 0) == root)
        run.note("focus", ok=focus_ok, error=err)
    run.lap("focus")

    stage = "paste"
    if not (front_ok and focus_ok):
        # --- 4. 本物のクリック ---
        fail_reason = R_NO_FOCUS if front_ok else R_FG_FAILED
        if not opts["allow_click"]:
            return run.done("undelivered", "front", fail_reason, readback=verdict)
        pt = click_point(elem_rect, win_rect, profile)
        if pt is None:
            return run.done("undelivered", "click", R_COVERED, readback=verdict, detail="クリックする点が決まらない")
        under, err = run.call("root_of_point", pt[0], pt[1])
        if err or int(under or 0) != root:
            run.note("click", ok=False, error=err or "点の下が宛先ではない")
            return run.done("undelivered", "click", R_COVERED, readback=verdict)
        c, err = run.call("click", pt[0], pt[1], root)
        clicked = bool((c or {}).get("clicked")) and not err
        run.note("click", ok=clicked, error=err or (c or {}).get("reason", ""))
        if not clicked:
            run.lap("click")
            return run.done("undelivered", "click", R_CLICK_FAILED, readback=verdict)
        ok, err2 = run.call("wait_foreground", root, opts["foreground_timeout_sec"])
        run.lap("click")
        if not ok or err2:
            return run.done("undelivered", "click", R_FG_FAILED, readback=verdict)
        stage = "click_paste"

    # --- 貼り付け（前面確認が取れた場合だけここへ来る）---
    snap, err = run.call("clipboard_snapshot")
    if err or snap is None:
        return run.done("undelivered", stage, R_CLIP_ERROR, detail=err)
    if not snap.get("restorable") and not opts["allow_unrestorable_clipboard"]:
        run.extra["clipboard_formats"] = list(snap.get("formats", []))[:10]
        return run.done("undelivered", stage, R_CLIP_NOT_RESTORABLE, readback=verdict)
    restored = None
    try:
        okc, err = run.call("clipboard_set_text", payload)
        if err or not okc:
            return run.done("undelivered", stage, R_CLIP_ERROR, detail=err)
        try:
            _guard_foreground(ops, root)      # キーを送る直前に、もう一度前面を確認
        except ForegroundNotConfirmed as e:
            return run.done("undelivered", stage, R_FG_LOST, detail=str(e), readback=verdict)
        except Exception as e:  # noqa: BLE001
            return run.done("undelivered", stage, R_OPS_ERROR, detail=f"{type(e).__name__}: {e}")
        pr, err = run.call("paste", root, mode)
        sent = bool((pr or {}).get("sent")) and not err
        run.note("paste", sent=sent, mode=mode, error=err or (pr or {}).get("reason", ""))
        if not sent:
            return run.done("undelivered", stage, R_PASTE_NOT_SENT, detail=err or (pr or {}).get("reason", ""),
                            readback=verdict)
        run.extra["keys_sent"] = True
        sleep(opts["paste_settle_sec"])
        verdict2, _a = _readback(run, root, profile, final, opts)
        run.note("readback", verdict=verdict2)
    finally:
        restored, _e = run.call("clipboard_restore", snap)
        run.extra["clipboard_restored"] = bool(restored)
        run.lap("paste")

    if verdict2 == "ok":
        _after_input_ok(run, root, profile, elem_rect, existing, opts, sleep, fw)
        return run.done("delivered", stage, readback="ok")
    if verdict2 == "mismatch" and opts["force_on_mismatch"]:
        run.note("force_on_mismatch", verdict=verdict2)       # 利用者が選んだ強制送信。ログに残す
        _after_input_ok(run, root, profile, elem_rect, existing, opts, sleep, fw)
        return run.done("delivered", stage, readback="mismatch_forced")
    return run.done("undelivered", stage, R_MISMATCH, readback=verdict2)


def _return_focus(run, root):
    """設定で有効なときだけ、前面化した場合に元の前面窓へ戻す（キー送信ではない）。"""
    orig = run.fg_before
    if orig and orig != root:
        run.call("bring_to_front", orig, None, "set_foreground")
        run.extra["returned_focus"] = True
