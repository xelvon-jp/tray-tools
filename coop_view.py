# coop_view.py
# 進捗パネルの「連携ステージ」に出す内容(いま誰待ちか)を決める純関数。Qt に依存しない。
#
# 材料は2つ: Muse 見張りの状態(muse_bridge_status.read の戻り)と、Claude のセッション
# 一覧(claude_status.list_sessions の行)。描く側(claude_progress)が now で呼び直すので、
# 経過時間は呼ぶたびに計算し直す。
import time

import muse_bridge_status as mbs

# tone の名前(色は描く側が持つ): work / claude / approval / alert / done / sleep
# tone ごとのキャラの動き: (Muse の anim, Claw'd の anim)
ANIMS = {
    "work": ("work", "watch"),
    "claude": ("claude", "work"),
    "approval": ("approval", "wait"),
    "alert": ("alert", "alert"),
    "done": ("done", "happy"),
    "sleep": ("sleep", "sleep"),
}
ITEMS = {"work": "fly", "approval": "parked", "alert": "wobble", "done": "back"}
PROPS = {"work": {"spark"}, "claude": {"dots"}, "alert": {"bang", "sweat"},
         "sleep": {"zz"}, "done": {"hat", "confetti"}}

CARD_APPROVAL_ALERT_SEC = 20      # 承認カードが見えないと見なす秒数
APPROVAL_ALERT_SEC = 180          # 承認待ちがこれ以上なら赤
NO_REPLY_ALERT_SEC = 60
UNKNOWN_ALERT_SEC = 60
MAILBOX_FAIL_LIMIT = 10
DONE_SHOW_SEC = 6                 # 完了の表示を出しておく秒数
MAIL_RECENT_SEC = 60


def _num(data, key):
    v = data.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _tool_name(name) -> str:
    name = str(name or "").strip()
    if name.startswith("mcp__") and name.count("__") >= 2:
        name = name.split("__")[-1]   # mcp__サーバー__ツール は末尾のツール名だけ
    return name


def _oldest(entries, key):
    """entries のうち key の時刻がいちばん古いもの(待たせている時間が長いもの)。"""
    def stamp(e):
        v = e.get("asked" if key == "asked" else "started")
        return v if isinstance(v, (int, float)) else float("inf")
    return min(entries, key=stamp)


def coop_view(muse, claude_entries, now=None):
    """連携ステージの表示内容(dict)。出さないなら None。

    banner_title / banner_sub / banner_time(経過を fmt_dur で。分からなければ空) /
    tone / muse_anim / clawd_anim / item(fly|parked|wobble|back|None) /
    props(set) / cards(3つの (ラベル, 値))。
    判定は上から順に勝つ(見張りの停止 > 郵便受け > 承認カード…)。"""
    if not isinstance(muse, dict):
        return None
    now = time.time() if now is None else now
    last_poll = _num(muse, "last_poll")
    if last_poll is None or now - last_poll > mbs.FORGET_SEC:
        return None
    entries = [e for e in (claude_entries or []) if isinstance(e, dict)]

    stopped = now - last_poll > mbs.STALE_SEC
    kind = muse.get("kind") if isinstance(muse.get("kind"), str) else None
    kind = kind or None
    prev_kind = muse.get("prev_kind")
    kind_since = _num(muse, "kind_since")
    kind_elapsed = max(0.0, now - kind_since) if kind_since is not None else None
    ke = kind_elapsed or 0.0
    cards_n = _num(muse, "cards")
    fail = _num(muse, "mailbox_fail") or 0
    header = muse.get("header")
    header = header.strip() if isinstance(header, str) else ""

    sub, tone, title, elapsed = "", "sleep", "", kind_elapsed
    running = [e for e in entries if e.get("state") == "running"]
    asking = [e for e in entries if e.get("state") == "ask"]

    if stopped:
        tone, title = "alert", "見張りが止まっています"
        sub = f"最後の確認 {mbs.fmt_dur(now - last_poll)}前"
        elapsed = now - last_poll
    elif muse.get("state") == "claude":
        # 見張りが Claude を起こして終了した。kind / cards は直前のまま残っているので
        # (approval で cards==0 など)、以降の判定は見ずに「Claude が確認中」とする。
        tone, title = "claude", "Claude が確認中"
        sub = "Muse の見張りが起こしました"
        since = _num(muse, "since")
        if since is None:
            since = kind_since
        elapsed = max(0.0, now - since) if since is not None else None
    elif fail >= MAILBOX_FAIL_LIMIT:
        tone, title = "alert", "郵便受けにつながりません"
        sub = "メールの取得に失敗しています"
    elif kind == "approval" and cards_n == 0 and ke >= CARD_APPROVAL_ALERT_SEC:
        tone, title, sub = "alert", "承認カードが見えません", "見張りが確認しています"
    elif kind == "approval":
        tone = "alert" if ke >= APPROVAL_ALERT_SEC else "approval"
        title, sub = "Muse が承認待ち", "Claw'd が見張って押します"
    elif kind == "no_reply" and ke >= NO_REPLY_ALERT_SEC:
        tone, title, sub = "alert", "Muse が返事をしていません", "Claude の発言に未返答です"
    elif kind == "unknown" and ke >= UNKNOWN_ALERT_SEC:
        tone, title, sub = "alert", "状態が分かりません", "Muse の画面を判定できません"
    elif kind in ("processing", "responding"):
        tone, title = "work", "いま：Muse が作業中"
        sub = header if header not in mbs.HEADER_PLAIN else "Claw'd が見守っています"
    elif asking:
        tone, title, sub = "approval", "Claude が承認待ち", "陽太さんの許可待ちです"
        asked = _oldest(asking, "asked").get("asked")
        elapsed = now - asked if isinstance(asked, (int, float)) else None
    elif running:
        top = _oldest(running, "started")
        tone, title = "claude", "いま：Claude が作業中"
        name = _tool_name(top.get("tool_name"))
        sub = (f"ツール実行中（{name}）" if top.get("phase") == "tool" and name
               else "Claw'd が手を動かしています")
        started = top.get("started")
        elapsed = now - started if isinstance(started, (int, float)) else None
    elif (prev_kind in ("processing", "responding", "approval")
          and kind == "idle" and kind_elapsed is not None
          and kind_elapsed < DONE_SHOW_SEC):
        tone, title, sub = "done", "完了しました", "Muse の作業が終わりました"
    else:
        tone, title, sub = "sleep", "全員待機中", "陽太さんの指示待ちです"

    muse_anim, clawd_anim = ANIMS[tone]
    return {
        "banner_title": title,
        "banner_sub": sub,
        "banner_time": mbs.fmt_dur(elapsed) if elapsed is not None else "",
        "tone": tone,
        "muse_anim": muse_anim,
        "clawd_anim": clawd_anim,
        "item": ITEMS.get(tone),
        "props": set(PROPS.get(tone, ())),
        "cards": _cards(muse, kind, ke, stopped, fail, now),
    }


def _cards(muse, kind, kind_elapsed, stopped, fail, now) -> list:
    approval_sec = _num(muse, "last_approval_sec")
    approved = _num(muse, "approved") or 0
    if kind == "approval":
        approval = mbs.fmt_dur(kind_elapsed)      # 承認待ちの間は経過秒
    elif approval_sec is not None:
        approval = f"{approval_sec:.1f}秒"
    elif approved > 0:
        approval = f"{int(approved)}件"
    else:
        approval = "—"
    mail_at = _num(muse, "last_mail_at")
    if fail > 0:
        mailbox = "取得失敗"
    elif mail_at is not None and 0 <= now - mail_at <= MAIL_RECENT_SEC:
        mailbox = f"受信 {int(now - mail_at)}秒前"
    else:
        mailbox = "空"
    watch = "停止" if stopped else ("不調" if fail > 0 else "正常")
    return [("承認", approval), ("郵便受け", mailbox), ("見張り", watch)]
