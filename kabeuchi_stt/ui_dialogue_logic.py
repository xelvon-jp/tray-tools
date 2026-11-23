# -*- coding: utf-8 -*-
"""AI対話モードのコンパクト窓: 画面に依存しない部分（純関数・小さなクラス）。Qt / COM 非依存。

窓（ui_dialogue.py）はここの結果を表示するだけにして、判定はここでテストする。
- 状態（待機・録音中・配達中・未配達あり・保留）→ 表示の文言と色の系統
- ヘッドセット押下 → 確定か再試行か（バッファ側の規則どおり）
- 宛先の表示（プリセット名・窓のプロセス名・タイトル、握った窓か見つからないか）
- 保存名のバリデーション
- 未配達一覧の操作（再試行・破棄）→ バッファ／セッションの呼び出し
"""
from __future__ import annotations

from dataclasses import dataclass

from dialogue_profile import sanitize_name

# ---------------------------------------------------------------------------
# 状態 → 表示
# ---------------------------------------------------------------------------

ST_STOPPED, ST_IDLE, ST_RECORDING, ST_DELIVERING, ST_UNDELIVERED, ST_HELD = (
    "stopped", "idle", "recording", "delivering", "undelivered", "held")

# 系統（tone）は窓側で ui_style の色に対応づける: muted / rec / accent / warn / ok
_STATE_TEXT = {
    ST_STOPPED: ("待機（セッション未開始）", "muted"),
    ST_IDLE: ("セッション中（マイクの入力待ち）", "ok"),
    ST_RECORDING: ("録音中", "rec"),
    ST_DELIVERING: ("配達中", "accent"),
    ST_UNDELIVERED: ("未配達あり", "warn"),
    ST_HELD: ("保留中（取り込みを止めています）", "warn"),
}


@dataclass
class StateView:
    key: str
    label: str
    tone: str


def view_state(snap: dict | None) -> StateView:
    """DialogueSession.snapshot() から表示用の状態を決める。優先順: 配達中 > 未配達あり > 保留 > 録音中 > 待機。
    未配達があっても録音は続くので、窓の「録音中」表示は別（recording_text）に出す。"""
    s = snap or {}
    if not s.get("active"):
        key = ST_STOPPED
    elif s.get("delivering"):
        key = ST_DELIVERING
    elif int(s.get("undelivered_count") or 0) > 0:
        key = ST_UNDELIVERED
    elif s.get("held"):
        key = ST_HELD
    elif s.get("recording"):
        key = ST_RECORDING
    else:
        key = ST_IDLE
    label, tone = _STATE_TEXT[key]
    if key == ST_UNDELIVERED:
        label = f"未配達 {int(s.get('undelivered_count') or 0)} 件"
    return StateView(key, label, tone)


def recording_text(snap: dict | None) -> tuple[str, str]:
    """録音中表示（常時）。(文言, tone)。録音中は赤。保留中は取り込みを止めていることを出す。"""
    s = snap or {}
    if not s.get("active"):
        return "● 録音していません", "muted"
    if not s.get("recording"):
        return "● マイクが止まっています", "warn"
    if s.get("held"):
        return "● 録音中（取り込み保留）", "warn"
    if s.get("speaking"):
        return "● 録音中（話しています）", "rec"
    pend = int(s.get("pending_recognitions") or 0)
    return ("● 録音中（認識待ち %d）" % pend if pend else "● 録音中"), "rec"


def recognition_view(snap: dict | None) -> tuple[str, str]:
    """認識の状態（常時見える場所に出す）。(文言, tone)。
    聞き取り中（発話あり）／清書待ち（○件）／待機。重なるときは並べて出す。"""
    s = snap or {}
    if not s.get("active"):
        return "認識: セッションが始まっていません", "muted"
    if s.get("held"):
        return "認識: 保留中（聞き取りも認識もしていません）", "warn"
    pend = max(0, int(s.get("pending_recognitions") or 0))
    speaking = bool(s.get("speaking"))
    parts = []
    if speaking:
        parts.append("聞き取り中（発話あり）")
    if pend:
        parts.append(f"清書待ち（{pend}件）")
    if not parts:
        return "認識: 待機（話しかけると区切って認識します）", "muted"
    return "認識: " + "・".join(parts), ("rec" if speaking else "accent")


def plan_draft_sync(prev: str, new: str) -> tuple[str, int, str]:
    """バッファの下書きが変わったとき、手で編集できる下書き欄をどう直すか。(操作, 削る文字数, 足す文字列)。
    - "none"    : 変わっていない
    - "append"  : 末尾へ足すだけ（認識結果の追記。編集中の内容・カーソル・選択を壊さない）
    - "patch"   : 末尾の1文字（「、」→「。」の置き換え）を削ってから足す（句点の補い）
    - "replace" : 全部入れ替え（確定・破棄で空になった、など）"""
    if new == prev:
        return "none", 0, ""
    c = 0
    for a, b in zip(prev, new):
        if a != b:
            break
        c += 1
    if c == len(prev):
        return "append", 0, new[len(prev):]
    if c > 0 and len(prev) - c <= 1:
        return "patch", len(prev) - c, new[c:]
    return "replace", 0, new


HEADSET_WEAK_NOTE = "動画の再生/一時停止も同時に反応する場合があります"


def headset_view(state: str, wanted: bool, reason: str = "", exclusive: bool = True) -> tuple[str, str]:
    """ヘッドセットボタンの表示。state は HeadsetButton.state（stopped/active/failed）。
    exclusive=False（HID だけで受付中）のときは弱い注意を添える。"""
    if not wanted:
        return "ヘッドセットボタン: 使わない設定", "muted"
    if state == "active":
        if not exclusive:
            return f"ヘッドセットボタン: 受付中（{HEADSET_WEAK_NOTE}）", "ok"
        return "ヘッドセットボタン: 受付中（再生/一時停止キーを独占）", "ok"
    if state == "failed":
        return "ヘッドセットボタン: 取得できません" + (f"（{reason}）" if reason else ""), "warn"
    return "ヘッドセットボタン: 停止（セッション中だけ受付）", "muted"


# ---------------------------------------------------------------------------
# 確定・再試行の分岐
# ---------------------------------------------------------------------------

PRESS_IGNORE, PRESS_BUSY, PRESS_RETRY, PRESS_COMMIT, PRESS_EMPTY = "ignore", "busy", "retry", "commit", "empty"


def press_action(snap: dict | None) -> str:
    """確定操作（ヘッドセット・窓のボタン・ホットキー）が何になるか。
    セッション外=ignore、配達中=busy（二重配達しない）、未配達あり=retry（新規の確定ではない）、
    下書きが空（話している途中・認識待ちも無い）=empty（何も届けない。押したことは画面に出す）、それ以外=commit。
    snap に draft_text が無いとき（古い形の辞書）は空かどうか判断せず commit とする。"""
    s = snap or {}
    if not s.get("active"):
        return PRESS_IGNORE
    if s.get("delivering"):
        return PRESS_BUSY
    if s.get("next_commit_is_retry") or int(s.get("undelivered_count") or 0) > 0:
        return PRESS_RETRY
    if ("draft_text" in s and not (s.get("draft_text") or "").strip() and not s.get("speaking")
            and int(s.get("pending_recognitions") or 0) == 0):
        return PRESS_EMPTY
    return PRESS_COMMIT


PRESS_MESSAGE = {
    PRESS_IGNORE: "セッションが始まっていません",
    PRESS_BUSY: "配達中です（終わるまでお待ちください）",
    PRESS_RETRY: "未配達を再試行します",
    PRESS_COMMIT: "確定して入力先へ届けます",
    PRESS_EMPTY: "下書きが空です",
}

_PRESS_VERB = {PRESS_COMMIT: "確定します", PRESS_RETRY: "再試行します", PRESS_EMPTY: "下書きが空です",
               PRESS_BUSY: "配達中です。終わるまで待ちます", PRESS_IGNORE: "セッションが始まっていません"}


def press_notice(source: str, action: str) -> tuple[str, str]:
    """確定操作を受けた瞬間の1行通知 (文言, tone)。ヘッドセット・ホットキーは、受け取ったことを必ず出す
    （押して無反応に見えないように）。窓のボタンは結果の通知が続くので、短くてよい。"""
    verb = _PRESS_VERB.get(action, "")
    tone = "warn" if action in (PRESS_EMPTY, PRESS_BUSY, PRESS_IGNORE) else "accent"
    if source == "headset":
        return f"ヘッドセットのボタンを受け取りました → {verb}", tone
    if source == "mouse":
        return f"マウスのボタンを受け取りました → {verb}", tone
    if source == "click":
        return f"舌打ちを検出しました → {verb}", tone
    if source == "hotkey":
        return f"確定ホットキーを受け取りました → {verb}", tone
    return PRESS_MESSAGE.get(action, ""), tone


# ---------------------------------------------------------------------------
# 宛先の表示
# ---------------------------------------------------------------------------

TG_NONE, TG_UNCHECKED, TG_HELD, TG_NOT_FOUND, TG_AMBIGUOUS, TG_ERROR = (
    "none", "unchecked", "held", "not_found", "ambiguous", "error")


@dataclass
class TargetView:
    state: str
    name: str
    process: str
    title: str
    status_text: str
    tone: str


def target_state_from_resolve(res: dict | None) -> str:
    """WorkerClient.resolve の結果 → 宛先の状態。"""
    st = (res or {}).get("status")
    return {"ok": TG_HELD, "none": TG_NOT_FOUND, "ambiguous": TG_AMBIGUOUS}.get(st, TG_ERROR)


def view_target(profile: dict | None, state: str = TG_UNCHECKED, resolve: dict | None = None,
                error: str = "") -> TargetView:
    """宛先の表示（プリセット名・プロセス名・窓のタイトル・握った窓か見つからないか）。
    入力先へ書く前に、どの窓へ届けるかをここで見せる。"""
    if not profile:
        return TargetView(TG_NONE, "", "", "", "宛先が未設定です（［宛先を選ぶ］かプリセットを選んでください）", "warn")
    name = str(profile.get("name") or "")
    proc = str(profile.get("process_name") or "")
    title = str(profile.get("captured_title") or profile.get("window_title_contains") or "")
    cands = (resolve or {}).get("candidates") or []
    if state == TG_HELD and cands:
        # 握った窓の実際のタイトル（候補に載っているもの）を優先して見せる
        held = int((resolve or {}).get("hwnd") or 0)
        for c in cands:
            if int(c.get("hwnd") or 0) == held and c.get("title"):
                title = str(c["title"])
    text, tone = {
        TG_HELD: ("握った窓へ届けます", "ok"),
        TG_UNCHECKED: ("窓を確認していません（セッション開始時に探します）", "muted"),
        TG_NOT_FOUND: ("窓が見つかりません（別の窓へは送りません）", "warn"),
        TG_AMBIGUOUS: ("同じ条件の窓が複数あります（宛先を選び直してください）", "warn"),
        TG_ERROR: ("窓を確認できませんでした" + (f"（{error}）" if error else ""), "warn"),
    }.get(state, ("", "muted"))
    return TargetView(state, name, proc, title, text, tone)


# ---------------------------------------------------------------------------
# 保存名のバリデーション
# ---------------------------------------------------------------------------

MAX_NAME_LEN = 40


@dataclass
class NameCheck:
    ok: bool
    name: str = ""          # 確定した名前（前後の空白を除いたもの）
    message: str = ""       # ok でないときの理由
    exists: bool = False    # 同名のプリセットがある（上書きの確認が要る）


def validate_preset_name(name: str, existing: list[str] | tuple[str, ...] = ()) -> NameCheck:
    """プリセット名として使えるか。ファイル名にできない文字・予約名は拒否し、勝手に別名へ変えない
    （sanitize_name で変わってしまう名前は、保存先が意図とずれるので通さない）。"""
    n = (name or "").strip()
    if not n:
        return NameCheck(False, "", "名前を入力してください")
    if len(n) > MAX_NAME_LEN:
        return NameCheck(False, n, f"名前は {MAX_NAME_LEN} 文字までです")
    if n.endswith((".", " ")) or sanitize_name(n) != n:
        return NameCheck(False, n, '使えない文字・名前です（ \\ / : * ? " < > | や予約名、末尾のピリオドは不可）')
    low = {str(e).lower() for e in existing}
    return NameCheck(True, n, "", n.lower() in low)


# ---------------------------------------------------------------------------
# 未配達・履歴の表示
# ---------------------------------------------------------------------------

REASON_TEXT = {
    "empty_text": "テキストが空でした",
    "no_target": "宛先が未設定です",
    "window_not_found": "宛先の窓が見つかりません",
    "window_ambiguous": "宛先の窓が複数あり決められません",
    "input_not_found": "入力欄が見つかりません",
    "existing_unreadable": "入力欄の書きかけを読めませんでした",
    "existing_text_abort": "入力欄に書きかけがあるため中止しました",
    "input_dirty": "入力欄が書き換わっていました",
    "user_active": "入力中のため前面化を見送りました",
    "foreground_failed": "窓を前面に出せませんでした",
    "foreground_lost": "前面の窓が変わったため中止しました",
    "focus_not_confirmed": "入力欄のフォーカスを確認できませんでした",
    "click_point_covered": "クリック位置の上に別の窓があります",
    "click_failed": "クリックできませんでした",
    "clipboard_not_restorable": "クリップボードを元に戻せないため貼り付けを見送りました",
    "clipboard_error": "クリップボードの操作に失敗しました",
    "paste_not_sent": "貼り付けを送れませんでした",
    "readback_mismatch": "入力後の読み戻しが一致しませんでした",
    "ops_error": "窓の操作でエラーが起きました",
    "busy_timeout": "相手が回答中のため送信しませんでした",
    "send_foreground_failed": "窓を前面に出せず、Enter を送れませんでした",
    "send_focus_not_confirmed": "入力欄のフォーカスを確認できず、Enter を送りませんでした",
    "send_foreground_lost": "前面の窓が変わったため、Enter を送りませんでした",
    "send_not_sent": "Enter を送れませんでした",
    "send_unconfirmed": "送信したか確認できませんでした",
}


def reason_text(reason: str) -> str:
    """配達側の理由コード（または文言）を、画面に出す日本語にする。未知のものはそのまま。"""
    r = (reason or "").strip()
    return REASON_TEXT.get(r, r or "理由不明")


def _hhmm(iso: str) -> str:
    s = iso or ""
    return s[11:16] if len(s) >= 16 else s


def snippet(text: str, n: int = 40) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def undelivered_rows(messages: list) -> list[dict]:
    """未配達の一覧の行。{id, time, reason, text, snippet}。古い順（バッファの順のまま）。"""
    rows = []
    for m in messages:
        rows.append({"id": m.id, "time": _hhmm(m.created_at), "reason": reason_text(m.reason),
                     "text": m.text, "snippet": snippet(m.text), "attempts": m.attempts})
    return rows


_STATUS_TEXT = {"delivered": "配達済み", "undelivered": "未配達", "delivering": "配達中", "discarded": "破棄"}


_SENT_TEXT = {"sent": "・送信済み", "not_sent": "・未送信", "unknown": "・送信は未確認"}

SEND_FAILED_NOTICE = "送信できませんでした（入力欄に残っています。Enter／送信ボタンを押してください）"


def delivered_notice(info: dict | None) -> tuple[str, str]:
    """配達できたときの案内 (文言, tone)。送信まで求めて送れなかったときは、押すべきものを出す。"""
    i = info or {}
    sent = i.get("sent") or ""
    reason = reason_text(str(i.get("send_reason") or "")) if i.get("send_reason") else ""
    if i.get("stage") == "record":
        return "記録しました（どこへも届けていません）", "ok"
    if sent == "sent":
        return "入力先へ届けて、送信しました", "ok"
    if sent == "not_sent":
        return SEND_FAILED_NOTICE + (f"［{reason}］" if reason else ""), "warn"
    if sent == "unknown":
        return "入力先へ届けました。送信されたか確認できませんでした（入力欄を見てください。二重には送りません）", "warn"
    return "入力先へ届けました（送信はしていません）", "ok"


def history_rows(messages: list) -> list[dict]:
    """確定メッセージの履歴の行（新しいものを下に）。{id, time, status, text, snippet}。
    宛先「なし（記録だけ）」で確定したものは、配達済みではなく「記録のみ」と出す。"""
    return [{"id": m.id, "time": _hhmm(m.created_at),
             "status": ((_STATUS_TEXT.get(m.status, m.status) + _SENT_TEXT.get(getattr(m, "sent", ""), ""))
                        if not (m.status == "delivered" and getattr(m, "stage", "") == "record") else "記録のみ")
                       + ("・手修正あり" if getattr(m, "edited", False) else ""),
             "text": m.text, "snippet": snippet(m.text, 60)} for m in messages]


def pick_guide_text(remaining_sec: float) -> str:
    """［宛先を選ぶ］の案内。クリック待ちの残り秒数つき。"""
    r = max(0, int(remaining_sec + 0.999))
    return f"入力欄をクリックしてください（あと {r} 秒。Esc でやめる）"


def pick_failure_text(res: dict | None) -> str:
    st = (res or {}).get("status")
    return {"cancelled": "宛先の選択をやめました", "timeout": "時間内にクリックされませんでした",
            "own_window": "この窓の中をクリックしています。入力先の窓をクリックしてください",
            "no_element": "クリックした場所の要素を取得できませんでした"}.get(st, f"宛先を取得できませんでした（{st or '不明'}）")


# ---------------------------------------------------------------------------
# 未配達の操作（窓のボタン → セッション／バッファ）
# ---------------------------------------------------------------------------

class UndeliveredOps:
    """窓の［再試行］［破棄］とセッション終了時の確認。session は DialogueSession（commit / buffer / is_active を持つもの）。
    クリップボードへは何も入れない（このツールが入れてよいのは、利用者が明示的に Ctrl+C／右クリックのコピーをしたときだけ）。"""

    def __init__(self, session):
        self.session = session

    @property
    def buffer(self):
        return self.session.buffer

    def retry(self) -> bool:
        """再試行。未配達があるときの確定操作は再試行になる（バッファ側の規則）。
        セッション外・未配達なし・配達中は何もしない（False）。"""
        b = self.buffer
        if not self.session.is_active or not b.has_undelivered or b.delivering:
            return False
        return bool(self.session.commit())

    def discard(self, ids: list[int] | None = None) -> int:
        """利用者が選んだ未配達を破棄する（履歴にはテキストが残る）。"""
        return self.buffer.discard_undelivered(ids)

    def discard_draft(self) -> str:
        return self.buffer.discard_draft()

    def resolve_end(self, choice: str, report: dict | None = None) -> dict:
        """セッション終了時の確認の結果を反映する。choice: discard / keep。
        discard は未配達と下書きを破棄。keep は何もしない（次のセッションへ持ち越す）。"""
        out = {"choice": choice, "discarded": 0, "draft_discarded": ""}
        if choice == "discard":
            out["discarded"] = self.discard()
            out["draft_discarded"] = self.discard_draft()
        return out


# ---------------------------------------------------------------------------
# 1列の画面（2026-11-22 再設計）: ヘッダー・下段の通知・ヘッドセットの診断表示
# ---------------------------------------------------------------------------

_TARGET_WARN = {
    TG_NONE: "宛先なし",
    TG_NOT_FOUND: "窓が見つかりません",
    TG_AMBIGUOUS: "同じ窓が複数あります",
    TG_ERROR: "窓を確認できません",
}


def target_header_warn(state: str) -> str:
    """ヘッダーの宛先の横に出す短い警告。見つかっているとき・まだ確認していないときは何も出さない（空文字）。
    プロセス名・窓のタイトル・「握った窓」などの状態語は出さない。"""
    return _TARGET_WARN.get(state, "")


def headset_header_warn(state: str, wanted: bool, reason: str = "") -> str:
    """ヘッドセットを使う設定なのに受け付けられていないときの、ヘッダーの短い警告（無ければ空文字）。
    セッションの外（stopped）では出さない。"""
    if wanted and state == "failed":
        return "⚠ ヘッドセット受付できません" + (f": {reason}" if reason else "")
    return ""


def headset_path_lines(paths: dict | None) -> list[str]:
    """検出経路ごとの状態の行（HID: 開けた／失敗の理由／押下回数、ホットキー: 登録結果／押下回数）。"""
    lines: list[str] = []
    for key, label, ok_word, ng_word in (("hid", "HID直接読み", "開けました", "開けません"),
                                         ("hotkey", "ホットキー", "登録しました", "登録できません")):
        p = (paths or {}).get(key)
        if not p:
            continue
        if not p.get("enabled"):
            lines.append(f"{label}: 使いません（設定）")
            continue
        det = p.get("detail") or {}
        if p.get("ok"):
            extra = f"押下 {int(p.get('received') or 0)} 回"
            if key == "hid":
                extra += f"・レポート {int(det.get('reports') or 0)} 件"
                if det.get("device"):
                    extra = f"{det['device']}・" + extra
                if det.get("last_report"):
                    extra += f"・最後: {det['last_report']}"
            if not p.get("alive"):
                extra += "・受信が止まりました" + (f"（{p['exit_reason']}）" if p.get("exit_reason") else "")
            lines.append(f"{label}: {ok_word}（{extra}）")
        elif p.get("ok") is False:
            why = p.get("reason") or "理由不明"
            code = f"・Win32 エラー {p['code']}" if p.get("code") else ""
            lines.append(f"{label}: {ng_word}（{why}{code}）")
        else:
            lines.append(f"{label}: まだ試していません")
    return lines


def headset_status_text(diag: dict | None, wanted: bool) -> str:
    """⚙メニュー・設定で見せるヘッドセットの独占の状態（複数行）。diag は HeadsetButton.diagnostics()。"""
    if not wanted:
        return "ヘッドセットのボタン: 使わない設定です"
    d = diag or {}
    state = d.get("state", "stopped")
    head = {"active": ("ヘッドセットのボタン: 受付中（再生/一時停止キーを独占）" if d.get("exclusive", True)
                       else f"ヘッドセットのボタン: 受付中（{HEADSET_WEAK_NOTE}）"),
            "failed": "ヘッドセットのボタン: 受け付けられません",
            }.get(state, "ヘッドセットのボタン: 停止中（セッション中だけ受け付けます）")
    lines = [head]
    if d.get("reason"):
        lines.append(f"理由: {d['reason']}")
    if d.get("register_code"):
        lines.append(f"ホットキー登録のエラーコード: {d['register_code']}")
    if d.get("mode"):
        lines.append(f"検出方式の設定: {d['mode']}（auto=HID直接読み＋ホットキー／hid／hotkey）")
    lines.extend(headset_path_lines(d.get("paths")))
    if state == "active":
        lines.append(f"受信スレッド: {'動作中' if d.get('thread_alive') else '停止（異常）'}")
    lines.append(f"押下 {int(d.get('presses') or 0)} 回（間引き {int(d.get('dropped') or 0)} 回）")
    if d.get("last_press"):
        lines.append(f"最後の押下: {d['last_press']}")
    return "\n".join(lines)


def mouse_status_line(diag: dict | None, button: str) -> str:
    """⚙「ヘッドセットの状態…」に添えるマウス経路の1行。button は設定 dialogue.mouse_button。"""
    label = {"back": "戻るボタン", "forward": "進むボタン"}.get(str(button or ""), "")
    if not label:
        return "マウスのボタン: 使わない設定です"
    d = diag or {}
    state = d.get("state", "stopped")
    if state == "active":
        return (f"マウスのボタン（{label}）: 受付中（押下 {int(d.get('presses') or 0)} 回・"
                f"セッション中だけ他のアプリへ渡さない）")
    if state == "failed":
        return f"マウスのボタン（{label}）: 受け付けられません" + (f"（{d['reason']}）" if d.get("reason") else "")
    return f"マウスのボタン（{label}）: 停止中（セッション中だけ受け付けます）"


def click_status_line(diag: dict | None, enabled: bool) -> str:
    """⚙「ヘッドセットの状態…」に添える舌打ち（2回で確定・実験）の1行。enabled は設定 dialogue.click_commit。"""
    if not enabled:
        return "舌打ち2回で確定: 無効（実験機能・設定でオン）"
    d = diag or {}
    state = d.get("state", "stopped")
    if state == "active":
        return f"舌打ち2回で確定: 有効・受付中（検出 {int(d.get('presses') or 0)} 回）"
    if state == "failed":
        return "舌打ち2回で確定: 受け付けられません" + (f"（{d['reason']}）" if d.get("reason") else "")
    return "舌打ち2回で確定: 有効（セッション中だけ受け付けます）"


class NoticeLine:
    """下段の1行の通知。時間で薄くなり、消える。警告（warn）は次の通知か clear まで残す。
    時計は呼び出し側が渡す（テストしやすいよう now を引数にする）。"""

    def __init__(self, hold_sec: float = 6.0, fade_sec: float = 4.0):
        self.hold_sec = float(hold_sec)
        self.fade_sec = float(fade_sec)
        self._text = ""
        self._tone = "muted"
        self._at = 0.0

    def post(self, text: str, tone: str = "muted", now: float = 0.0) -> None:
        self._text, self._tone, self._at = text or "", tone or "muted", float(now)

    def clear(self) -> None:
        self._text = ""

    def sticky(self) -> bool:
        return bool(self._text) and self._tone == "warn"

    def view(self, now: float, fallback: tuple[str, str] = ("", "muted")) -> tuple[str, str, bool]:
        """(文言, tone, 薄くしているか)。通知が無い・時間切れのときは fallback（常時の短い案内）を出す。"""
        if not self._text:
            return fallback[0], fallback[1], False
        if self._tone == "warn":
            return self._text, "warn", False
        age = float(now) - self._at
        if age < self.hold_sec:
            return self._text, self._tone, False
        if age < self.hold_sec + self.fade_sec:
            return self._text, self._tone, True
        self._text = ""
        return fallback[0], fallback[1], False


IDLE_HINT = "Ctrl+Enter でも確定"


def idle_hint_text(methods=None) -> str:
    """常時の案内。methods は、いま有効な確定の手段の名前（「ヘッドセット」「マウス」「舌打ち」）。使っていない手段は出さない。"""
    names = [m for m in (methods or []) if m]
    return "Ctrl+Enter／" + "／".join(names) + "でも確定" if names else IDLE_HINT


def idle_hint(snap: dict | None, undelivered_extra: int = 0, methods=None) -> tuple[str, str]:
    """通知が無いときに下段へ出す常時の案内。未配達があればその件数（警告色）。"""
    s = snap or {}
    n = int(s.get("undelivered_count") or 0)
    if n > 0:
        return f"未配達 {n}件", "warn"
    if not s.get("active"):
        return "", "muted"
    return idle_hint_text(methods), "muted"


def delivered_short(info: dict | None, hhmm: str) -> tuple[str, str]:
    """配達できたときの下段の1行 (文言, tone)。成功は短く（時刻つき）、送信できなかったときは押すべきものを残す（warn）。"""
    i = info or {}
    sent = i.get("sent") or ""
    reason = reason_text(str(i.get("send_reason") or "")) if i.get("send_reason") else ""
    if i.get("stage") == "record":
        return f"記録しました ✓ {hhmm}", "ok"
    if sent == "sent":
        return f"届けて送信しました ✓ {hhmm}", "ok"
    if sent == "not_sent":
        return SEND_FAILED_NOTICE + (f"［{reason}］" if reason else ""), "warn"
    if sent == "unknown":
        return "届けました。送信されたか確認できませんでした（入力欄を見てください。二重には送りません）", "warn"
    return f"入力欄に入れました ✓ {hhmm}", "ok"


def compose_with_prefix(prefix: str, text: str) -> str:
    """実際に送られる文字列 = 前置き本文 ＋ 改行 ＋ 下書き。前置きが空なら下書きのまま（何も足さない）。"""
    pre = (prefix or "").rstrip("\r\n")
    return f"{pre}\n{text}" if pre.strip() else text


SEND_HEAD_SENDING = "送信中…"
SEND_HEAD_RECORD_DONE = "✓ 記録しました"
SEND_HEAD_SENT = "✓ 送信しました"
SEND_HEAD_DELIVERED = "✓ 届けました"


SEND_HEAD_UNCONFIRMED = "⚠ 届きました。送信を確認できませんでした（入力欄を見てください）"


def send_unconfirmed(info: dict | None) -> bool:
    """届いたが、送信されたか確認できなかったか（赤字で残し続ける）。宛先なし（記録のみ）は対象外。"""
    i = info or {}
    return i.get("stage") != "record" and (i.get("sent") or "") == "unknown"


def send_done_head(info: dict | None) -> str:
    """配達が済んだときの、送信中ブロックの見出し。宛先「なし」は「記録しました」、送信まで済めば「送信しました」、
    送信を確認できなかったときは赤い警告、入力欄へ入れただけ（送信しない設定）なら「届けました」。"""
    i = info or {}
    if i.get("stage") == "record":
        return SEND_HEAD_RECORD_DONE
    if (i.get("sent") or "") == "sent":
        return SEND_HEAD_SENT
    if send_unconfirmed(i):
        return SEND_HEAD_UNCONFIRMED
    return SEND_HEAD_DELIVERED


def waiting_recognition_text(n: int) -> str:
    """確定のとき、清書待ちの発話があって待っているあいだの通知。"""
    return f"認識を待っています（{int(n)}件）"


# ---------------------------------------------------------------------------
# 下書き欄（1つの編集可能な欄）の中身: 確定済み（白）の後ろに、区切れて清書待ちの発話ごとの「認識中…」（小さな波形）が並ぶ
# ---------------------------------------------------------------------------

K_WAVE = "wave"            # 区切れて、清書待ちの発話（長さ固定のバー＋「認識中…」）
K_LIVE = "live"            # いま喋っている途中の発話（バーが喋った長さに比例して右へ伸びる。末尾に1つだけ）
TAIL_KINDS = (K_WAVE, K_LIVE)
LIVE_UID = 1e12            # 末尾に置くための ID（清書待ちの発話の ID＝開始秒より必ず後ろ）


@dataclass(frozen=True)
class TailItem:
    """欄の後ろ側（まだ白になっていない発話）の1つ。uid は発話の ID（区切りの開始秒）。
    wave: 波形の棒の高さ（0〜8）。label: バーの右に出す文字（喋っている途中のとき「喋っています 0:12」）。"""
    uid: float
    kind: str = K_WAVE
    wave: tuple = ()
    label: str = ""


def build_tail(waves: list[dict], live: dict | None = None) -> list[TailItem]:
    """まだ文字になっていない発話（{uid, env}）から、欄の後ろ側の並びを作る。並びは uid（時刻）順。
    live（{env, label}。いま喋っている途中の発話）があれば、一番後ろに1つ足す。"""
    items: dict[float, TailItem] = {}
    for w in waves or []:
        items[float(w["uid"])] = TailItem(float(w["uid"]), K_WAVE, tuple(w.get("env") or ()), str(w.get("label") or ""))
    out = [items[k] for k in sorted(items)]
    if live and live.get("env"):
        out.append(TailItem(LIVE_UID, K_LIVE, tuple(live["env"]), str(live.get("label") or "")))
    return out


@dataclass(frozen=True)
class CurBlock:
    """いま欄の後ろ側にあるブロック（読み取り用）。"""
    uid: float
    kind: str = K_WAVE
    wave: tuple = ()
    label: str = ""


def plan_tail(cur: list[CurBlock], new: list[TailItem]) -> list[tuple]:
    """欄の後ろ側を cur から new へ直す操作の列（ブロック単位。前から順に適用する）。
    ("remove", i) / ("insert", i, item) / ("update", i, item)。i はその時点の後ろ側の何番目か。
    同じ uid で中身が同じなら何もしない（ユーザーのカーソル・選択を動かさない）。"""
    ops: list[tuple] = []
    work = list(cur)
    i = j = 0
    while i < len(work) or j < len(new):
        if j >= len(new):
            ops.append(("remove", i))
            del work[i]
        elif i >= len(work):
            ops.append(("insert", i, new[j]))
            work.insert(i, CurBlock(new[j].uid, new[j].kind, new[j].wave, new[j].label))
            i += 1
            j += 1
        else:
            c, n = work[i], new[j]
            if c.uid == n.uid:
                if c.kind != n.kind or tuple(c.wave) != tuple(n.wave) or c.label != n.label:
                    ops.append(("update", i, n))
                    work[i] = CurBlock(n.uid, n.kind, n.wave, n.label)
                i += 1
                j += 1
            elif c.uid < n.uid:
                ops.append(("remove", i))
                del work[i]
            else:
                ops.append(("insert", i, n))
                work.insert(i, CurBlock(n.uid, n.kind, n.wave, n.label))
                i += 1
                j += 1
    return ops


def wave_levels(audio, bars: int = 24, levels: int = 8) -> tuple:
    """音声（int16 の numpy 配列）→ 波形の棒の高さ（0〜levels）。棒ごとの最大振幅を、音量差が見えるよう
    平方根で圧縮する。空なら全部 0。"""
    import numpy as np
    bars = max(1, int(bars))
    a = np.abs(np.asarray(audio, dtype=np.float32)) if audio is not None and len(audio) else np.zeros(0, np.float32)
    if a.size == 0:
        return tuple([0] * bars)
    edges = np.linspace(0, a.size, bars + 1).astype(int)
    out = []
    for k in range(bars):
        seg = a[edges[k]:max(edges[k + 1], edges[k] + 1)]
        v = float(seg.max()) / 32768.0 if seg.size else 0.0
        out.append(int(round(min(1.0, v ** 0.5) * levels)))
    return tuple(out)


def live_wave(audio, bars: int = 24, frame: int = 1600, levels: int = 8) -> tuple:
    """話している途中の音（int16）→ 直近 bars 本の棒（1本＝frame サンプル＝0.1秒。右端が最新）。
    音が bars 本に満たないときは、左を 0 で埋める（棒が右から伸びていき、流れる）。高さの付け方は wave_levels と同じ。"""
    import numpy as np
    bars, frame = max(1, int(bars)), max(1, int(frame))
    a = np.abs(np.asarray(audio, dtype=np.float32)) if audio is not None and len(audio) else np.zeros(0, np.float32)
    n = bars * frame
    buf = np.zeros(n, dtype=np.float32)
    if a.size:
        a = a[-n:]
        buf[n - a.size:] = a
    peaks = buf.reshape(bars, frame).max(axis=1) / 32768.0
    return tuple(int(round(min(1.0, float(v) ** 0.5) * levels)) for v in peaks)


WAVE_PER_SEC = 6          # 1秒あたりの棒の数（バーの長さ＝喋った秒数に比例）
WAVE_ROW_SEC = 10         # バーの1行あたりの秒数（欄の幅に収まる長さ。右端に達したら次の行へ折り返す。ここを変えれば折り返しの長さが変わる）
WAVE_ROW_BARS = WAVE_ROW_SEC * WAVE_PER_SEC


def wave_bars_for(duration_sec: float) -> int:
    """発話の長さに応じた棒の数（長さに比例。折り返しても上限は設けない）。「認識中…」のバーと、喋っている途中のバー（live_bars）で同じ尺度。"""
    return max(2, int(round(float(duration_sec) * WAVE_PER_SEC)))


def live_bars(audio, sr: int = 16000) -> tuple:
    """いま喋っている途中の音（int16）→ バーの棒の高さ。1本＝1/WAVE_PER_SEC 秒。すでにある棒は変わらず、右へ（行末で折り返して次の行へ）伸びる。
    1/WAVE_PER_SEC 秒に満たなければ空。"""
    n = 0 if audio is None else len(audio)
    frame = max(1, int(sr / WAVE_PER_SEC))
    count = n // frame
    if count < 1:
        return ()
    return wave_levels(audio[:count * frame], count)
