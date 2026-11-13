# muse_bridge_status.py
# Claude が Muse の画面を見張っている間の状態(何を待っていて、どのくらい経ったか)を
# トレイのツールチップに出すための読み取り側。
#
# 書くのは見張り役(Claude 側の監視スクリプト)で、常駐はファイルを読むだけ
# (常駐に UIA を持ち込まない。CLAUDE.md / feature_screen の方針どおり)。
# Qt に依存しない純関数にしてあり、tests/test_muse_bridge_status.py で確かめる。
#
# 状態ファイル(JSON)の項目:
#   state         "waiting" | "busy" | "settling" | "idle" | "claude"
#   since         いまの state になった時刻(epoch 秒)
#   waiting_since Muse の返事を待ち始めた時刻(epoch 秒)。待っていなければ null
#   poll_sec      画面を読む間隔(秒)
#   last_poll     最後に画面を読んだ時刻(epoch 秒)
#   settle, settle_need  返事が落ち着いたと見なすまでの回数(いま/必要)
#   approved      自動で押した承認カードの数
#   kind          Muse の画面から見た状態。"idle"(待機) | "responding"(応答中) |
#                 "processing"(処理中) | "approval"(承認待ち) | "no_reply"(Claude の
#                 発言に未返答) | "unknown"(判定不能)。古いファイルには無い
#   kind_since    いまの kind になった時刻(epoch 秒)
#   header        Muse の画面の見出し(「実装中」など。自由文で null のこともある)
# kind があればラベルと経過時間はそちらを基準にする。無ければ state / since で従来どおり。
#
#   muse_self     Muse の自己申告(郵便受け kind=status を見張りが受けて転記したもの)。
#                 {"state": "working" | "waiting_approval" | "waiting_user" |
#                  "idle" | "done", "summary": 一言, "at": 受けた時刻(epoch 秒)}。
#                 表示は画面判定(kind)を土台に上乗せし、食い違いは注意表示にする
#                 (どちらも黙って正にしない)。60分より古い申告は無いものとして扱う。
#
#   requests      取り次ぎ依頼(req-YYYYMMDD-NN)の進み具合。見張りが @ミドリ の
#                 依頼と、ミドリの返事(受領／完了／失敗の書き出しの行)を見て
#                 書き込む列(形式の正本は見張り側)。新しい順に最大10件。
#                 要素は {"id", "title"(短い件名。最大40字), "stage", "times",
#                 "updated"} と、失敗のときだけ "reason"(失敗の理由)。
#                 任意で "kind"("task" | "note"。note は印だけの依頼で、
#                 バーに出さない)、"ack_wait_sec"(受領までの秒数)、
#                 "late"(受領まで10分以上。真偽値)、"done_at"(完了時刻)。
#                 stage は "detected"(検知) | "saved"(保存) | "ack"(受領) |
#                 "done"(完了) | "failed"(失敗)。送信は見張りからは見えない
#                 ので stage には無い(バーの段階としては1段目に数える)。
#                 times は届いた段階だけを持つ {"detected": 時刻, ...} の形
#                 (時刻は epoch 秒)。updated は最後に更新した時刻(epoch 秒)。
#                 表示への変換は request_rows(純関数)。
#
# 使い道は2つ: ツールチップの行(describe)と、進捗パネルの Muse 見張り行
# (panel_view。バーの割合まで含めてここで計算し、描画は claude_progress が担う)。
import json
import os
import time
from pathlib import Path

STATUS_PATH = (Path(os.environ.get("LOCALAPPDATA") or ".")
               / "traytools" / "muse_bridge_status.json")

# 最後に画面を読んでからこれ以上経ったら、見張りは止まっていると見なす。
STALE_SEC = 90
# 止まってからこれ以上経った古いファイルは、もう出さない(昨日の残りを見せない)。
FORGET_SEC = 6 * 3600

LABELS = {
    "waiting": "返事待ち",
    "busy": "作業中",
    "settling": "返事の確定待ち",
    "idle": "待機中",
    "claude": "Claude確認中",
}

# kind → ラベル(短く)。processing だけ見出しを括弧で添える(_kind_label)。
KIND_LABELS = {
    "idle": "Muse 待機（指示待ち）",
    "responding": "Muse 応答中",
    "processing": "Muse 処理中",
    "approval": "Muse 承認待ち",
    "no_reply": "Muse 未応答",
    "unknown": "Muse 状態不明",
}
# 見出しがこれ(または空)のときは括弧で添えない。
HEADER_PLAIN = ("リンク済み", "")
HEADER_MAX = 12
# tone の切り替え秒数。
WARN_APPROVAL_SEC = 60
ALERT_APPROVAL_SEC = 180
ALERT_NO_REPLY_SEC = 60
ALERT_UNKNOWN_SEC = 60


def read(path=None):
    """状態ファイルを読む。無い・壊れているなら None(表示が出ないだけ)。"""
    try:
        with open(Path(path or STATUS_PATH), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def fmt_dur(sec) -> str:
    """経過秒を「4分12秒」「1時間5分」「8秒」の形にする。"""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}時間{m}分"
    if m:
        return f"{m}分{s:02d}秒"
    return f"{s}秒"


# 状態ファイルが書き込み中などで読めなかったとき、直前の良いデータを保持する最大秒数。
HOLD_SEC = 10


def hold_last_good(new, last, last_at, now, hold=HOLD_SEC):
    """読めなかった(None)ときに直前の良いデータを最大 hold 秒だけ使い回す純関数。

    戻り値は (表示に使うデータ, 控えのデータ, 控えた時刻)。new が dict なら更新して
    それを返す。dict でなければ、控えが hold 秒以内ならそれを、過ぎたら None を返す
    (控えは残すだけで、期限を過ぎたものは使わない)。"""
    if isinstance(new, dict):
        return new, new, now
    if isinstance(last, dict) and last_at is not None and 0 <= now - last_at <= hold:
        return last, last, last_at
    return None, last, last_at


def _num(data, key):
    v = data.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _kind_label(kind, header) -> str:
    label = KIND_LABELS.get(kind, KIND_LABELS["unknown"])
    if kind == "processing" and isinstance(header, str):
        header = header.strip()
        if header not in HEADER_PLAIN:
            if len(header) > HEADER_MAX:
                header = header[:HEADER_MAX] + "…"
            label += f"（{header}）"
    return label


def _tone(kind, elapsed) -> str:
    """calm(落ち着き) / active(動いている) / warn(そろそろ) / alert(要対応)。"""
    e = elapsed or 0
    if kind in ("responding", "processing"):
        return "active"
    if kind == "approval":
        if e >= ALERT_APPROVAL_SEC:
            return "alert"
        return "warn" if e >= WARN_APPROVAL_SEC else "calm"
    if kind == "no_reply":
        return "alert" if e >= ALERT_NO_REPLY_SEC else "warn"
    if kind == "unknown":
        return "alert" if e >= ALERT_UNKNOWN_SEC else "calm"
    return "calm"


def _kind_info(data, now):
    """(kind, kind_label, kind_elapsed, tone)。kind が無い古いファイルは全部 None / calm。

    kind が文字列でも知らない値なら unknown として扱う。"""
    kind = data.get("kind")
    if not isinstance(kind, str) or not kind:
        return None, None, None, "calm"
    if kind not in KIND_LABELS:
        kind = "unknown"
    since = _num(data, "kind_since")
    elapsed = max(0.0, now - since) if since is not None else None
    return kind, _kind_label(kind, data.get("header")), elapsed, _tone(kind, elapsed)


# ---- Muse の自己申告(kind=status の転記) ----
SELF_STALE_SEC = 3600   # これより古い申告は無いものとして扱う(状態提供の原則)
SELF_QUIET_SEC = 600    # 作業中・承認待ちの申告がこの秒数を超えて無更新なら注意

SELF_LABELS = {
    "working": "作業中",
    "waiting_approval": "承認待ち",
    "waiting_user": "ユーザー待ち",
    "idle": "待機中",
    "done": "完了",
}
# 自己申告と画面判定を比べるための粗い分類。None は「比べない」。
_SELF_GROUPS = {"working": "active", "waiting_approval": "approval",
                "waiting_user": "idle", "idle": "idle", "done": "idle"}
_KIND_GROUPS = {"responding": "active", "processing": "active",
                "approval": "approval", "idle": "idle"}


def self_report(data, now=None):
    """自己申告(muse_self)を表示用に解決する。無い・壊れている・古い(>60分)なら None。

    戻り値: {"state", "label", "summary", "age", "group"}。group は画面判定と
    比べるための分類(active / approval / idle / None)。"""
    if not isinstance(data, dict):
        return None
    raw = data.get("muse_self")
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    if not isinstance(state, str) or not state:
        return None
    at = None
    for key in ("at", "received_at", "ts"):
        at = _num(raw, key)
        if at is not None:
            break
    if at is None:
        return None
    now = time.time() if now is None else now
    age = max(0.0, now - at)
    if age > SELF_STALE_SEC:
        return None
    summary = raw.get("summary")
    return {
        "state": state,
        "label": SELF_LABELS.get(state, state),
        "summary": summary.strip() if isinstance(summary, str) else "",
        "age": age,
        "group": _SELF_GROUPS.get(state),
    }


def self_caution(data, now=None):
    """自己申告と見張りの判定の食い違い(注意文)。無ければ None。

    ①分類が食い違う(申告=作業中なのに画面は待機、など)。画面判定が
    no_reply / unknown のときは比べない(判定不能を食い違いにしない)。
    ②申告が作業中・承認待ちのまま SELF_QUIET_SEC を超えて無更新。"""
    now = time.time() if now is None else now
    rep = self_report(data, now)
    if rep is None:
        return None
    kind, kind_label, _elapsed, _tone_name = _kind_info(data, now)
    kind_group = _KIND_GROUPS.get(kind) if kind else None
    if rep["group"] and kind_group and rep["group"] != kind_group:
        return (f"自己申告と見張りの判定が食い違っています"
                f"(申告: {rep['label']} / 見張り: {kind_label})")
    if rep["group"] in ("active", "approval") and rep["age"] > SELF_QUIET_SEC:
        return f"自己申告(「{rep['label']}」)が {fmt_dur(rep['age'])} 更新されていません"
    return None


def describe(data, now=None) -> list:
    """ツールチップに足す行(0〜2行)を返す。

    1行目: 何を待っていて何分経ったか。2行目: 見張りの間隔・次に読むまで・押したカード。
    見張りが止まっているときは1行だけ(止まっていることと、最後の状態)。"""
    if not data:
        return []
    now = time.time() if now is None else now
    state = data.get("state")
    label = LABELS.get(state, str(state or "?"))
    since = _num(data, "since")
    waiting_since = _num(data, "waiting_since")
    last_poll = _num(data, "last_poll")
    poll_sec = _num(data, "poll_sec")

    if last_poll is None or now - last_poll > STALE_SEC:
        if last_poll is None or now - last_poll > FORGET_SEC:
            return []
        return [f"Muse監視 停止中（{fmt_dur(now - last_poll)}前・最後は{label}）"]

    kind, kind_label, kind_elapsed, _tone_name = _kind_info(data, now)
    in_state = f" {fmt_dur(now - since)}" if since is not None else ""
    if kind is not None:
        head = kind_label + (f" {fmt_dur(kind_elapsed)}" if kind_elapsed is not None else "")
    elif waiting_since is not None and state != "waiting":
        head = f"Muse返事待ち {fmt_dur(now - waiting_since)}（{label}{in_state}）"
    elif waiting_since is not None:
        head = f"Muse返事待ち {fmt_dur(now - waiting_since)}"
    else:
        head = f"Muse {label}{in_state}"
    if state == "settling" and kind is None:
        settle, need = _num(data, "settle"), _num(data, "settle_need")
        if settle is not None and need:
            head += f" {int(settle)}/{int(need)}"

    parts = []
    if poll_sec and poll_sec > 0:
        nxt = max(0, int(round(last_poll + poll_sec - now)))
        parts.append(f"{int(poll_sec)}秒毎・次{nxt}秒")
    approved = _num(data, "approved")
    if approved:
        parts.append(f"自動承認{int(approved)}件")
    lines = [head]
    if parts:
        lines.append("監視 " + "・".join(parts))
    return lines


def panel_view(data, now=None):
    """進捗パネルの Muse 見張り行に出す値(now 基準)。行を出さないなら None。

    - データが無い・last_poll が無い/ FORGET_SEC(6時間)より古い → None
    - last_poll が STALE_SEC(90秒)より古い → stopped(灰色の「監視停止」行)
    バーの割合: poll_fraction は次に画面を読むまでの残り(1→0)、
    settle_fraction は確定待ちの段階(返事の確定待ちのときだけ)。
    kind / kind_label / kind_elapsed / tone は kind があるときの表示用(無ければ
    None / None / None / "calm"。描く側は label / elapsed に戻る)。stopped が
    True のときは描く側が tone より灰色を優先する。"""
    if not data:
        return None
    now = time.time() if now is None else now
    last_poll = _num(data, "last_poll")
    if last_poll is None or now - last_poll > FORGET_SEC:
        return None
    state = data.get("state")
    since = _num(data, "since")
    waiting_since = _num(data, "waiting_since")
    poll_sec = _num(data, "poll_sec")
    stopped = now - last_poll > STALE_SEC
    kind, kind_label, kind_elapsed, tone = _kind_info(data, now)
    rep = self_report(data, now)
    view = {
        "self_label": rep["label"] if rep else None,
        "self_summary": rep["summary"] if rep else "",
        "self_age": rep["age"] if rep else None,
        "caution": self_caution(data, now),
        "kind": kind,
        "kind_label": kind_label,
        "kind_elapsed": kind_elapsed,
        "tone": tone,
        "state": state,
        "label": LABELS.get(state, str(state or "?")),
        "stopped": stopped,
        "elapsed": max(0.0, now - since) if since is not None else None,
        "waiting": (max(0.0, now - waiting_since)
                    if waiting_since is not None else None),
        "approved": int(_num(data, "approved") or 0),
        "poll_fraction": None,
        "poll_next_sec": None,
        "settle_fraction": None,
        "settle_text": None,
        "stale_sec": max(0.0, now - last_poll) if stopped else None,
    }
    if not stopped and poll_sec and poll_sec > 0:
        remain = last_poll + poll_sec - now
        view["poll_next_sec"] = max(0, int(round(remain)))
        view["poll_fraction"] = min(1.0, max(0.0, remain / poll_sec))
    if state == "settling":
        settle, need = _num(data, "settle"), _num(data, "settle_need")
        if settle is not None and need:
            view["settle_fraction"] = min(1.0, max(0.0, settle / need))
            view["settle_text"] = f"{int(settle)}/{int(need)}"
    return view


# ---- 取り次ぎ依頼(requests)の進み具合 ----
# バーは5段(送信→検知→保存→受領→完了)。送信はデータに無いので検知が
# 2/5 から始まる。失敗は満タンで、色は描く側が失敗の色にする。
REQ_FRACTIONS = {"detected": 0.4, "saved": 0.6, "ack": 0.8,
                 "done": 1.0, "failed": 1.0}
REQ_STAGE_LABELS = {"detected": "検知", "saved": "保存", "ack": "受領",
                    "done": "完了", "failed": "失敗"}
REQ_MAX_ACTIVE = 5      # 進行中は新しい順にこの数まで
REQ_MAX_FINISHED = 3    # 終わったばかりの完了は新しい順にこの数まで
REQ_DONE_COLLAPSE_SEC = 180   # 完了はこの秒数を過ぎたら畳んだ1行にまとめる
REQ_DONE_TTL_SEC = 24 * 3600  # 完了はこの秒数を過ぎたら出さない(見張りの保持と揃える)
REQ_LATE_SEC = 600      # 受領までこの秒数以上で「遅延」(見張りの late と同じ)
# 段階の間の目安(開始の段階, 終わりの段階, 秒数)。待ちの間は経過で、
# 次の段階に届いたあとは記録された間で判定する。
REQ_SPANS = (("detected", "saved", 60),
             ("saved", "ack", 60),
             ("ack", "done", 30 * 60))
REQ_NO_ACK_RED_SEC = REQ_LATE_SEC  # 受領が無く遅延(late)になったら赤(red)


def _no_ack_late(entry, now, earliest, late):
    """受領がまだ無い段階で遅延かどうか。赤くするかは late だけで決める
    (2026-11-13、アオの指定)。見張りの late / ack_wait_sec があればそれを
    使い、無ければ検知からの経過で判定する。"""
    if isinstance(entry.get("late"), bool):
        return entry["late"]
    w = _num(entry, "ack_wait_sec")
    if w is None:
        w = now - earliest
    return w >= REQ_LATE_SEC


def _req_row(entry, now):
    """requests の要素1つを表示用の行にする。形が合わなければ None。"""
    rid = entry.get("id")
    stage = entry.get("stage")
    if not isinstance(rid, str) or not rid or stage not in REQ_STAGE_LABELS:
        return None
    raw_times = entry.get("times")
    if not isinstance(raw_times, dict):
        return None
    times = {}
    for name in ("detected", "saved", "ack", "done"):
        v = raw_times.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            times[name] = v
    if not times:
        return None
    updated = _num(entry, "updated")
    finished = stage in ("done", "failed")
    # 終わった時刻(done_at → times の done → updated の順。欠けたら他方で代用)
    ended = _num(entry, "done_at") or times.get("done") or updated
    if ended is None:
        ended = max(times.values())
    earliest = min(times.values())   # 送信は無いので、最初の検知時刻が起点
    # 受領までの時間と遅延か(見張りの値があればそれを使い、無ければ times から)
    ack_wait = _num(entry, "ack_wait_sec")
    if ack_wait is None and "ack" in times:
        ack_wait = times["ack"] - earliest
    late = entry.get("late")
    if not isinstance(late, bool):
        late = ack_wait is not None and ack_wait >= REQ_LATE_SEC
    reason = entry.get("reason")
    reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
    # 受領のあと止まっている(見張り v48 が requests に付ける)。表示は late と
    # 同じ薄い警告に留める(2026-11-13 の討論、テーマ14)。
    stalled = entry.get("stalled") is True
    warn, note = "none", ""
    if stage == "done":
        # 完了したら注意の色は戻す。受領の遅れだけ小さな印で残す(2026-11-13 合意)
        if late and ack_wait is not None:
            note = f"受領まで{fmt_dur(ack_wait)}"
    else:
        for a_key, b_key, limit in REQ_SPANS:
            a = times.get(a_key)
            if a is None:
                continue
            b = times.get(b_key)
            if b is None and finished and b_key == "done":
                b = ended   # 失敗は終わりの時刻で受領からの間を判定する
            if b is not None:
                gap, waiting = b - a, False
            elif not finished:
                gap, waiting = now - a, True
            else:
                continue
            if gap > limit:
                warn = "warn"
                note = (f"{REQ_STAGE_LABELS[a_key]}から{REQ_STAGE_LABELS[b_key]}まで "
                        f"{fmt_dur(gap)}" + (" 経過" if waiting else " かかりました"))
                break
        if not finished and "ack" not in times and _no_ack_late(entry, now, earliest, late):
            warn = "red"
            note = "受領が10分以上来ていません"
        if not finished and stalled and warn == "none":
            warn = "warn"
            note = "受領のあと止まっています"
        if stage == "failed" and not note:
            note = reason or ""
    stage_at = ended if finished else times.get(stage)
    if finished or stage_at is None:
        label = REQ_STAGE_LABELS[stage]
    else:
        label = f"{REQ_STAGE_LABELS[stage]} {fmt_dur(now - stage_at)}"
    title = entry.get("title")
    return {
        "id": rid,
        "title": title.strip() if isinstance(title, str) else "",
        "stage": stage,
        "stage_label": REQ_STAGE_LABELS[stage],
        "fraction": REQ_FRACTIONS[stage],
        "warn": warn,
        "label": label,
        "note": note,
        "fail_reason": reason if stage == "failed" else None,
        "finished": finished,
        "kind": entry.get("kind") if entry.get("kind") in ("task", "note")
                else "task",
        "ack_wait_sec": ack_wait,
        "late": bool(late),
        "stalled": stalled,
        "sent_at": earliest,
        "ended_at": ended if finished else None,
        # ✕で閉じた判定のキー。段階が進めば(キーが変われば)また出る。
        "key": (rid, stage, stage_at),
    }


def request_rows(data, now=None) -> list:
    """取り次ぎ依頼の進捗行(描く側がそのまま使える形)を返す純関数。

    並びは 進行中 → 失敗 → 終わったばかりの完了 → 畳んだ完了の1行。
    進行中は新しい順に REQ_MAX_ACTIVE 件、終わったばかりの完了
    (REQ_DONE_COLLAPSE_SEC 以内)は新しい順に REQ_MAX_FINISHED 件まで出し、
    それより古い完了は「完了 N件(最後: …)」の1行(stage="summary")に
    畳んで一番下に出す。失敗は畳まず、データにある限り残す(消すのは
    見る人)。kind="note"(印だけの依頼)はバーに出さない。
    見張りが止まって FORGET_SEC より古いファイルは丸ごと出さない。
    (表示の切り分けは 2026-11-13 にアオと合意)"""
    if not isinstance(data, dict):
        return []
    now = time.time() if now is None else now
    last_poll = _num(data, "last_poll")
    if last_poll is not None and now - last_poll > FORGET_SEC:
        return []
    raw = data.get("requests")
    if not isinstance(raw, list):
        return []
    active, failed, done_fresh, done_pool = [], [], [], []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == "note":
            continue
        row = _req_row(entry, now)
        if row is None:
            continue
        if row["stage"] == "done":
            ended = row["ended_at"] if row["ended_at"] is not None \
                else row["sent_at"]
            age = now - ended
            if age > REQ_DONE_TTL_SEC:
                continue
            (done_fresh if age <= REQ_DONE_COLLAPSE_SEC
             else done_pool).append(row)
        elif row["finished"]:
            failed.append(row)
        else:
            active.append(row)

    def _by_ended(rows):
        return sorted(rows, key=lambda r: -(r["ended_at"]
                                            if r["ended_at"] is not None
                                            else r["sent_at"]))

    active.sort(key=lambda r: -r["sent_at"])
    failed = _by_ended(failed)
    done_fresh = _by_ended(done_fresh)
    done_pool.extend(done_fresh[REQ_MAX_FINISHED:])
    done_fresh = done_fresh[:REQ_MAX_FINISHED]
    rows = active[:REQ_MAX_ACTIVE] + failed + done_fresh
    if done_pool:
        done_pool = _by_ended(done_pool)
        last = done_pool[0]
        late_n = sum(1 for r in done_pool if r["late"])
        rows.append({
            # id は固定の "summary"(最後の依頼の id を借りると、行のキーが
            # 実在の依頼と重なって描く側が混同する。2026-11-13 の討論、テーマ3)
            "id": "summary",
            "title": f"完了 {len(done_pool)}件（最後: "
                     f"{last['title'] or last['id']}）",
            "stage": "summary",
            "stage_label": "完了",
            "fraction": 1.0,
            "warn": "none",
            "label": "完了",
            "note": f"うち遅延{late_n}件" if late_n else "",
            "fail_reason": None,
            "finished": True,
            "kind": "task",
            "ack_wait_sec": None,
            "late": False,
            "stalled": False,
            "sent_at": last["sent_at"],
            "ended_at": last["ended_at"],
            # ✕で閉じた判定のキー。畳んだ中身が変われば(キーが変われば)また出る。
            "key": ("__done_summary__", len(done_pool),
                    last["id"], last["ended_at"]),
        })
    return rows
