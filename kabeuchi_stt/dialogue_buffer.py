# -*- coding: utf-8 -*-
"""AI対話モード: メッセージ・バッファと未配達キュー（純ロジック。GUI/Qt/COM 非依存）。

流れ:  発話(認識済みテキスト＝清書) → 下書き(draft) → [確定] → メッセージ → 配達 → delivered / undelivered
（仮認識＝清書の前の暫定の文字は廃止した。下書きに入るのは、区切りごとの清書（small など）の結果だけ）

守ること（テストで保証）:
- **確定したテキストは、どの経路でも捨てない。** 確定した瞬間にメッセージとして履歴に登録し、配達の結果が
  どうなっても（失敗・例外・中断・保存と復元）履歴に残る。未配達は期限で消えない。
  メッセージの状態が discarded になるのは、利用者が破棄を選んだときだけ（それでも履歴にテキストは残る）。
- 未配達があるときの確定操作は、新規の確定ではなく「再試行」になる（下書きはそのまま残る）。
  古いものから順に配達する。設定 merge_undelivered ならまとめて1通で再試行する。
- 保留（hold）中は発話を取り込まない（確定済みのものには影響しない）。
- 配達中（delivering）に確定操作を重ねても、二重に配達しない。
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from dialogue_text import compose_draft, finalize_text

UNDELIVERED, DELIVERING, DELIVERED, DISCARDED = "undelivered", "delivering", "delivered", "discarded"
MODE_COMMIT, MODE_RETRY = "commit", "retry"

WAITING_REASON = "前の未配達の再試行待ち"
INTERRUPTED_REASON = "中断（前回の配達が完了しませんでした）"


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def join_texts(parts: list[str]) -> str:
    """発話を連結する。日本語は区切りなし、英数字どうしが隣り合うときだけ半角スペースを入れる。"""
    out = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and p[0].isascii() and p[0].isalnum():
            out += " "
        out += p
    return out


@dataclass
class Message:
    id: int
    text: str
    created_at: str
    status: str = DELIVERING
    reason: str = ""            # 未配達の理由
    attempts: int = 0
    stage: str = ""             # 配達できた段階（uia / paste など。配達側が返したもの）
    last_attempt_at: str = ""
    delivered_at: str = ""
    utterances: int = 1
    edited: bool = False        # 下書きを手で修正してから確定した（履歴の印）
    sent: str = ""              # 送信の状態: ""=送信まではしない設定 / "sent" / "not_sent"（入力欄に残っている）/ "unknown"
    send_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeliveryRequest:
    """配達の依頼。text を配達し、結果を DialogueBuffer.report(request, result) へ返す。"""
    mode: str                   # "commit"（新規の確定）/ "retry"（未配達の再試行）
    ids: list[int]
    text: str
    merged: bool = False        # 複数の未配達を1通にまとめたか


@dataclass
class DeliveryOutcome:
    delivered: bool
    reason: str = ""
    stage: str = ""
    detail: dict = field(default_factory=dict)
    sent: str = ""              # "" / "sent" / "not_sent" / "unknown"（配達できたときだけ意味がある）
    send_reason: str = ""


SENT, NOT_SENT, SEND_UNKNOWN = "sent", "not_sent", "unknown"


def _sent_state(v: Any, requested: bool = True) -> str:
    """配達側の sent（True / False / "unknown" / None）→ 状態。キー自体が無ければ送信は求めていない。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return SEND_UNKNOWN if v.lower() == "unknown" else (SENT if v.lower() in ("sent", "true") else NOT_SENT)
    return SENT if v else NOT_SENT

def normalize_result(result: Any) -> DeliveryOutcome:
    """配達側の返り値を判定する。**status が "delivered"（または True）のときだけ成功。** それ以外・例外・None は未配達。"""
    if isinstance(result, DeliveryOutcome):
        return result
    if isinstance(result, bool):
        return DeliveryOutcome(result, "" if result else "未配達（理由不明）")
    if isinstance(result, dict):
        ok = result.get("status") == DELIVERED
        reason = str(result.get("reason") or "")
        return DeliveryOutcome(ok, "" if ok else (reason or "未配達（理由不明）"),
                               str(result.get("stage") or ""), dict(result),
                               sent=_sent_state(result.get("sent")) if ok else "",
                               send_reason=str(result.get("send_reason") or "") if ok else "")
    status = getattr(result, "status", None)
    if status is not None:
        ok = status == DELIVERED
        reason = str(getattr(result, "reason", "") or "")
        return DeliveryOutcome(ok, "" if ok else (reason or "未配達（理由不明）"),
                               str(getattr(result, "stage", "") or ""))
    return DeliveryOutcome(False, "配達側から結果が返りませんでした")


class DialogueBuffer:
    def __init__(self, merge_undelivered: bool = False, now: Callable[[], str] = _now_iso,
                 on_change: Callable[[str], None] | None = None,
                 gap_sec: float | None = None, force_gap_sec: float | None = None,
                 punctuate_on_commit: bool = False, sentence_newline: bool = False, blank_line: bool = False):
        """gap_sec / force_gap_sec: 発話の間の長さで句点・改行を入れる規則（dialogue_text。None=従来どおりの連結）。
        punctuate_on_commit: 確定のとき、末尾が句読点で終わっていなければ「。」を補う。"""
        self.merge_undelivered = bool(merge_undelivered)
        self.gap_sec = gap_sec
        self.force_gap_sec = force_gap_sec
        self.punctuate_on_commit = bool(punctuate_on_commit)
        self.sentence_newline = bool(sentence_newline)    # 文中の句点系の直後に改行を入れる
        self.blank_line = bool(blank_line)                # 無音で区切った所は改行2つ（前が句点系のとき）
        self._now = now
        self.on_change = on_change
        self._lock = threading.RLock()
        self._draft: list[dict] = []       # {"text", "t", "start_sec"}
        self._messages: list[Message] = []  # セッション履歴（確定順。捨てない）
        self._next_id = 1
        self._held = False
        self._rev = 0                      # 下書きが変わるたびに増える（画面が差分を追うため）

    # ---- 通知（ロックの外で呼ぶ）
    def _notify(self, what: str) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb(what)
            except Exception:
                pass

    # ---- 保留
    @property
    def held(self) -> bool:
        return self._held

    def hold(self, on: bool = True) -> None:
        with self._lock:
            changed = self._held != bool(on)
            self._held = bool(on)
        if changed:
            self._notify("hold")

    # ---- 下書き
    def _compose_locked(self) -> str:
        """確定済み部分（清書＋手修正）だけの文章。句点・改行の規則はここに適用する。"""
        return compose_draft(self._draft, self.gap_sec, self.force_gap_sec, newline_after_sentence=self.sentence_newline,
                             blank_line=self.blank_line)

    def _final_text_locked(self) -> str:
        """確定するときの文章（下書き全体の末尾に、句点の補いを行う）。"""
        return finalize_text(self._compose_locked(), self.punctuate_on_commit)

    def add_utterance(self, text: str, start_sec: float | None = None, end_sec: float | None = None,
                      uid: float | None = None, allow_held: bool = False) -> bool:
        """認識結果（清書: small）を下書きへ足す。保留中・空文字は取り込まない（False）。
        allow_held=True のときは、保留中でも足す（保留に入る前に話し終えて認識に回した発話。捨てない）。
        start_sec / end_sec（録音の頭からの秒）があれば、直前の発話との間の長さで句点・改行を入れる。
        uid（発話の ID）は呼び出し側の識別用（以前は仮認識の置き換えに使った。いまは使わない）。"""
        text = (text or "").strip()
        with self._lock:
            added = False
            if (allow_held or not self._held) and text:
                self._draft.append({"text": text, "t": self._now(), "start_sec": start_sec, "end_sec": end_sec})
                self._rev += 1
                added = True
        if added:
            self._notify("draft")
        return added

    def begin_session(self) -> None:
        """新しいセッションを始める。前のセッションから持ち越した下書き（確定済みの白い文字）はそのまま残す（文字は捨てない）。
        発話の ID（録音の頭からの秒）はセッションごとに 0 から数え直す。"""
        return None

    @property
    def draft_final_text(self) -> str:
        """確定済み部分（清書＋手修正）。下書き欄（手で直せる）に出すのはこれ。"""
        with self._lock:
            return self._compose_locked()

    @property
    def draft_rev(self) -> int:
        """下書きの版。認識の追記・手修正・破棄・確定のたびに増える。"""
        with self._lock:
            return self._rev

    @property
    def draft_edited(self) -> bool:
        with self._lock:
            return any(d.get("edited") for d in self._draft)

    def set_draft_text(self, text: str, base_rev: int | None = None) -> bool:
        """下書きを手で修正した内容へ置き換える（キーボード編集の反映）。以後の add_utterance は、この内容の末尾へ連結される。
        - 前後の空白は除く。いまの下書きと同じなら何もしない（版も変えない・手修正の印も付けない）。True。
        - 空にしたら下書きを空にする（discard_draft と同じ）。
        - base_rev を渡したとき、下書きの版がそれと違えば**反映しない**（False）。画面が見ていない認識結果が
          途中で届いていたということなので、画面側は差分を取り込んでからやり直す（届いた認識結果を上書きで失わない）。
        戻り値: 反映した（または同じ内容だった）なら True、版の食い違いで断ったなら False。"""
        new = (text or "").strip()
        with self._lock:
            if base_rev is not None and base_rev != self._rev:
                return False
            if new == self._compose_locked():
                return True
            start = next((d.get("start_sec") for d in self._draft if d.get("start_sec") is not None), None)
            end = next((d.get("end_sec") for d in reversed(self._draft) if d.get("end_sec") is not None), None)
            self._draft = ([{"text": new, "t": self._now(), "start_sec": start, "end_sec": end, "edited": True}]
                           if new else [])
            self._rev += 1
        self._notify("draft")
        return True

    @property
    def draft_text(self) -> str:
        """下書き全体。確定するとこれが送られる（draft_final_text と同じ）。"""
        with self._lock:
            return self._compose_locked()

    @property
    def draft_utterances(self) -> list[dict]:
        with self._lock:
            return [dict(d) for d in self._draft]

    @property
    def has_draft(self) -> bool:
        with self._lock:
            return bool(self._draft)

    def discard_draft(self) -> str:
        """未確定の下書きを破棄する（確定済みには触れない）。破棄した文字列を返す。"""
        with self._lock:
            text = self._compose_locked()
            had_final = bool(self._draft)
            self._draft = []
            if had_final:
                self._rev += 1
        if text:
            self._notify("draft")
        return text

    # ---- 確定・再試行
    def commit(self, auto: bool = False) -> DeliveryRequest | None:
        """確定操作。戻り値は配達の依頼（なければ None）。
        - 配達中: None（二重配達しない）。
        - 未配達あり: **再試行**（下書きは確定しない）。auto=True（自動確定）のときは再試行はせず、
          下書きを未配達の列の末尾へ並べる（enqueue_draft。順序を保ち、あとの再試行でまとめて/順に配達できる）。
        - 未配達なし: 下書きを確定してメッセージにし、その配達を依頼する。下書きが空なら None。"""
        with self._lock:
            if self._delivering_locked():
                return None
            if self._undelivered_locked():
                if auto:
                    queued = self._enqueue_draft_locked(WAITING_REASON)
                    req = None
                else:
                    req = self._begin_retry_locked()
                    queued = None
            else:
                queued = None
                req = self._begin_commit_locked()
        if req is not None:
            self._notify("commit" if req.mode == MODE_COMMIT else "retry")
        elif queued is not None:
            self._notify("undelivered")
        return req

    def enqueue_draft(self, reason: str = "") -> "Message | None":
        """下書きを確定するが配達は試さず、未配達の列の末尾に並べる（配達中でも可）。下書きが空なら None。"""
        with self._lock:
            m = self._enqueue_draft_locked(reason or WAITING_REASON)
        if m is not None:
            self._notify("undelivered")
        return m

    def _take_draft_locked(self, status: str, reason: str = "") -> "Message | None":
        """下書き全体を確定してメッセージにする。"""
        text = self._final_text_locked()
        if not text:
            return None
        m = Message(self._next_id, text, self._now(), status, reason=reason,
                    utterances=len(self._draft), edited=any(d.get("edited") for d in self._draft))
        self._next_id += 1
        self._draft = []
        self._rev += 1
        self._messages.append(m)
        return m

    def _enqueue_draft_locked(self, reason: str) -> "Message | None":
        return self._take_draft_locked(UNDELIVERED, reason)

    def begin_retry(self) -> DeliveryRequest | None:
        """未配達を（古いものから）再試行する依頼を作る。"""
        with self._lock:
            if self._delivering_locked():
                return None
            req = self._begin_retry_locked()
        if req is not None:
            self._notify("retry")
        return req

    def _begin_commit_locked(self) -> DeliveryRequest | None:
        m = self._take_draft_locked(DELIVERING)     # 配達の前に履歴へ入れる（配達が何をしても残る）
        if m is None:
            return None
        self._mark_attempt(m)
        return DeliveryRequest(MODE_COMMIT, [m.id], m.text)

    def _begin_retry_locked(self) -> DeliveryRequest | None:
        pending = self._undelivered_locked()
        if not pending:
            return None
        if self.merge_undelivered and len(pending) > 1:
            for m in pending:
                self._mark_attempt(m)
            return DeliveryRequest(MODE_RETRY, [m.id for m in pending],
                                   "\n".join(m.text for m in pending), merged=True)
        m = pending[0]
        self._mark_attempt(m)
        return DeliveryRequest(MODE_RETRY, [m.id], m.text)

    def _mark_attempt(self, m: Message) -> None:
        m.status, m.reason = DELIVERING, ""
        m.attempts += 1
        m.last_attempt_at = self._now()

    # ---- 配達結果
    def report(self, request: DeliveryRequest, result: Any) -> DeliveryOutcome:
        """配達の結果を反映する。delivered 以外（例外オブジェクト含む）はすべて未配達として保持する。"""
        if isinstance(result, BaseException):
            out = DeliveryOutcome(False, f"配達中に例外: {type(result).__name__}: {result}")
        else:
            try:
                out = normalize_result(result)
            except Exception as e:  # 想定外の形でもテキストは捨てない
                out = DeliveryOutcome(False, f"結果を解釈できません: {e}")
        with self._lock:
            for m in self._messages:
                if m.id in request.ids and m.status == DELIVERING:
                    if out.delivered:
                        m.status, m.delivered_at, m.stage, m.reason = DELIVERED, self._now(), out.stage, ""
                        m.sent, m.send_reason = out.sent, out.send_reason
                    else:
                        m.status, m.reason = UNDELIVERED, out.reason
        self._notify("delivered" if out.delivered else "undelivered")
        return out

    def abort(self, request: DeliveryRequest, reason: str) -> None:
        """配達を始められなかった・中断した。テキストは未配達として保持する。"""
        self.report(request, DeliveryOutcome(False, reason))

    # ---- 未配達
    def _delivering_locked(self) -> bool:
        return any(m.status == DELIVERING for m in self._messages)

    def _undelivered_locked(self) -> list[Message]:
        return [m for m in self._messages if m.status == UNDELIVERED]   # 確定順 = 古い順

    @property
    def delivering(self) -> bool:
        with self._lock:
            return self._delivering_locked()

    @property
    def undelivered(self) -> list[Message]:
        with self._lock:
            return [Message(**m.to_dict()) for m in self._undelivered_locked()]

    @property
    def undelivered_count(self) -> int:
        with self._lock:
            return len(self._undelivered_locked())

    @property
    def has_undelivered(self) -> bool:
        return self.undelivered_count > 0

    @property
    def next_commit_is_retry(self) -> bool:
        """次の確定操作が「再試行」になるか（ヘッドセットの案内・窓の表示用）。"""
        return self.has_undelivered

    def copy_text(self, kind: str = "undelivered", sep: str = "\n") -> str:
        """手で貼るためのテキスト。kind = "undelivered"（古い順）/ "draft" / "all"（未配達＋下書き）。"""
        with self._lock:
            u = sep.join(m.text for m in self._undelivered_locked())
            d = self._compose_locked()
        if kind == "undelivered":
            return u
        if kind == "draft":
            return d
        return sep.join(p for p in (u, d) if p)

    def discard_undelivered(self, ids: list[int] | None = None) -> int:
        """利用者が破棄を選んだ未配達を discarded にする（履歴にテキストは残る）。破棄した件数を返す。"""
        n = 0
        with self._lock:
            for m in self._messages:
                if m.status == UNDELIVERED and (ids is None or m.id in ids):
                    m.status = DISCARDED
                    n += 1
        if n:
            self._notify("discard")
        return n

    # ---- 履歴・終了時
    @property
    def history(self) -> list[Message]:
        with self._lock:
            return [Message(**m.to_dict()) for m in self._messages]

    def end_report(self) -> dict:
        """セッション終了時の確認用。未配達が残っていれば［コピー］［破棄］を選ばせる。
        draft_text は確定していない下書き（確定されないまま終わった分）。"""
        with self._lock:
            und = [m.to_dict() for m in self._undelivered_locked()]
            draft = self._compose_locked()
            inflight = [m.id for m in self._messages if m.status == DELIVERING]
        return {"undelivered": und, "undelivered_count": len(und), "draft_text": draft,
                "delivering_ids": inflight, "needs_attention": bool(und or draft or inflight)}

    # ---- 保存
    def to_dict(self) -> dict:
        with self._lock:
            return {"version": 1, "next_id": self._next_id, "held": self._held,
                    "messages": [m.to_dict() for m in self._messages],
                    "draft": [dict(d) for d in self._draft]}

    @classmethod
    def from_dict(cls, d: dict, **kw) -> "DialogueBuffer":
        b = cls(**kw)
        msgs = []
        for x in (d or {}).get("messages", []):
            m = Message(**{k: x[k] for k in Message.__dataclass_fields__ if k in x})
            if m.status == DELIVERING:   # 保存時に配達の途中だったものは、結果不明なので未配達として残す
                m.status, m.reason = UNDELIVERED, INTERRUPTED_REASON
            msgs.append(m)
        b._messages = msgs
        b._draft = [dict(x) for x in (d or {}).get("draft", [])]
        b._held = bool((d or {}).get("held", False))
        b._rev = 1
        b._next_id = max([int((d or {}).get("next_id", 1))] + [m.id + 1 for m in msgs])
        return b
