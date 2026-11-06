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


def _num(data, key):
    v = data.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


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

    in_state = f" {fmt_dur(now - since)}" if since is not None else ""
    if waiting_since is not None and state != "waiting":
        head = f"Muse返事待ち {fmt_dur(now - waiting_since)}（{label}{in_state}）"
    elif waiting_since is not None:
        head = f"Muse返事待ち {fmt_dur(now - waiting_since)}"
    else:
        head = f"Muse {label}{in_state}"
    if state == "settling":
        settle, need = _num(data, "settle"), _num(data, "settle_need")
        if settle is not None and need:
            head += f" {int(settle)}/{int(need)}"

    parts = []
    if poll_sec:
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
    settle_fraction は確定待ちの段階(返事の確定待ちのときだけ)。"""
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
    view = {
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
    if not stopped and poll_sec:
        remain = last_poll + poll_sec - now
        view["poll_next_sec"] = max(0, int(round(remain)))
        view["poll_fraction"] = min(1.0, max(0.0, remain / poll_sec))
    if state == "settling":
        settle, need = _num(data, "settle"), _num(data, "settle_need")
        if settle is not None and need:
            view["settle_fraction"] = min(1.0, max(0.0, settle / need))
            view["settle_text"] = f"{int(settle)}/{int(need)}"
    return view
