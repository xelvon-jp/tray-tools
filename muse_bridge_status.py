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
