# coop_view.py
# 進捗パネルの「連携ステージ」に出す内容(いま誰待ちか)を決める純関数。Qt に依存しない。
#
# 材料は2つ: Muse 見張りの状態(muse_bridge_status.read の戻り)と、Claude のセッション
# 一覧(claude_status.list_sessions の行)。描く側(claude_progress)が now で呼び直すので、
# 経過時間は呼ぶたびに計算し直す。
import time

import bridge_events_view as bev
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
# item は状態の表現: parked(承認待ちで止まった手紙)と wobble(異常で揺れる)
# は描く側が今も使う。fly / back は状態の名前として残してあるが、飛行そのものは
# ループ再生をやめ、実イベント駆動(view の flights)に置き換えた(work-board#8)。
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


PB_LABELS = {
    "traytools_sync": "tray-tools 同期",
    "daemon_sync": "配備係の同期",
    "desk_sync": "窓口の同期",
    "deploy_approve": "配備の承認",
    "config_set": "設定の変更",
    "traytools_restart": "常駐の再起動",
    "watcher_sync": "見張りの同期",
    "pc_capture": "画面の撮影",
}
PB_RUN_STALE_SEC = 600            # 実行中がこれより古ければ合図ファイルの残骸とみなす
PB_DONE_SHOW_SEC = 60             # 完了・失敗の結果を出しておく秒数
PB_NUMBERS = {
    "traytools_sync": 1,
    "daemon_sync": 2,
    "desk_sync": 3,
    "deploy_approve": 4,
    "config_set": 5,
    "traytools_restart": 6,
    "watcher_sync": 7,
    "pc_capture": 8,
}


def _pb_title(name, suffix):
    """「プレイブック(PB6)実行中」の形。番号表に無い PB は番号なしで出す。"""
    num = PB_NUMBERS.get(name)
    return f"プレイブック(PB{num}){suffix}" if num else f"プレイブック{suffix}"


def _pb_view(desk, now):
    """窓口の合図ファイル (desk_activity) からプレイブック表示を作る。無ければ None。"""
    if not isinstance(desk, dict):
        return None
    name = desk.get("playbook")
    label = PB_LABELS.get(name, name) if isinstance(name, str) and name else None
    if not label:
        return None
    rid = desk.get("request_id")
    who = f"{label}（{rid}）" if isinstance(rid, str) and rid else label
    started = _num(desk, "started_at")
    if desk.get("state") == "running":
        if started is None or now - started > PB_RUN_STALE_SEC:
            return None
        return {"tone": "work", "title": _pb_title(name, "実行中"), "sub": who,
                "elapsed": max(0.0, now - started)}
    if desk.get("state") == "done":
        finished = _num(desk, "finished_at")
        if finished is None or not (0 <= now - finished <= PB_DONE_SHOW_SEC):
            return None
        dur = finished - started if started is not None else None
        summary = desk.get("summary")
        sub = f"{label}: {summary}" if isinstance(summary, str) and summary else label
        if desk.get("ok"):
            return {"tone": "done", "title": _pb_title(name, "完了"), "sub": sub,
                    "elapsed": dur}
        return {"tone": "alert", "title": _pb_title(name, "失敗"), "sub": sub,
                "elapsed": dur}
    return None


def coop_view(muse, claude_entries, now=None, paused=False, events=None, desk=None):
    """連携ステージの表示内容(dict)。出さないなら None。

    banner_title / banner_sub / banner_time(経過を fmt_dur で。分からなければ空) /
    tone / muse_anim / clawd_anim / item(fly|parked|wobble|back|None) /
    props(set) / cards(3つの (ラベル, 値)) / all_idle / paused /
    flights(実イベントで飛んでいる手紙・受領。bridge_events_view) /
    tape(履歴テープの行)。events はやり取りの記録(bridge_journal)の
    レコード列で、渡さなければ flights / tape は空になる。
    all_idle は「全員待機中」(Muse も Claude も陽太さん待ち)のときだけ True で、
    描く側がブリッジをゆっくり明滅させる目印。paused はブリッジから見張りの
    休止が指示されているとき(bridge_control の合図。判定より優先して出す)。
    判定は上から順に勝つ(休止 > 見張りの停止 > 郵便受け > 承認カード…)。"""
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
    # kind_since が無い(kind だけ書かれている)ときは since → waiting_since で経過を出す
    kind_since = _num(muse, "kind_since")
    if kind_since is None:
        kind_since = _num(muse, "since")
    if kind_since is None:
        kind_since = _num(muse, "waiting_since")
    kind_elapsed = max(0.0, now - kind_since) if kind_since is not None else None
    ke = kind_elapsed or 0.0
    cards_n = _num(muse, "cards")
    fail = _num(muse, "mailbox_fail") or 0
    header = muse.get("header")
    header = header.strip() if isinstance(header, str) else ""

    sub, tone, title, elapsed = "", "sleep", "", kind_elapsed
    all_idle = False
    running = [e for e in entries if e.get("state") == "running"]
    asking = [e for e in entries if e.get("state") == "ask"]
    pb = _pb_view(desk, now)

    if paused:
        # 休止中は last_poll が進まなくても「止まっている」とは言わない
        # (止めたのは陽太さん自身で、理由が分かっているため)。
        tone, title, sub, elapsed = ("sleep", "見張り休止中",
                                     "ブリッジのボタンで再開できます", None)
    elif stopped:
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
    elif pb is not None:
        tone, title, sub, elapsed = pb["tone"], pb["title"], pb["sub"], pb["elapsed"]
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
        all_idle = True
        # 経過は「全員が待機になってから」。Muse だけの待機時間(kind_elapsed)だと、
        # Claude が今終わったばかりでも長く見えてしまう。Claude 側の最後の更新と比べて短い方を採る。
        ts = [e.get("updated") for e in entries if isinstance(e.get("updated"), (int, float))]
        if ts:
            since = now - max(ts)
            elapsed = since if elapsed is None else min(elapsed, since)

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
        "cards": _cards(muse, kind, ke, stopped, fail, now, paused),
        "all_idle": all_idle,
        "paused": bool(paused),
        "self": mbs.self_report(muse, now),
        "caution": mbs.self_caution(muse, now),
        "flights": bev.flights(events, now) if events else [],
        "tape": bev.tape_lines(events, now) if events else [],
    }


def _cards(muse, kind, kind_elapsed, stopped, fail, now, paused=False) -> list:
    approval_sec = _num(muse, "last_approval_sec")
    if approval_sec is not None:
        approval_sec = max(0.0, approval_sec)
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
    route_name = {"home": "家", "worker": "私書箱"}.get(muse.get("route"))
    if route_name:
        mailbox = f"{route_name}・{mailbox}"
    watch = "休止" if paused else ("停止" if stopped else ("不調" if fail > 0 else "正常"))
    return [("承認", approval), ("郵便受け", mailbox), ("見張り", watch)]
