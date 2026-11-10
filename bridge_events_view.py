# bridge_events_view.py
# 連携ステージの「実イベントで飛ぶ手紙」と「履歴テープ」の材料を、やり取りの
# 記録(bridge_journal のレコード列)から作る純関数。Qt に依存しない。
#
# いままでは手紙・受領は状態に連動したループ再生で、実際のやり取りとは
# 関係なく飛んでいた(work-board#8)。ここではイベントの epoch と現在時刻
# だけで「いま飛んでいるもの」と「テープの行」を決める。同じ入力には
# 常に同じ結果を返すので、読み直しのたびに二重に飛ぶことはない。
# 状態の表現(承認待ちで止まった手紙=parked、異常で揺れる=wobble)は
# coop_view / coop_stage 側に残してあり、ここは実イベントの分だけを見る。
import time

FLIGHT_SEC = 2.0      # 1回の飛行にかける秒数
PRESS_SEC = 0.5       # approval の「押す動き」を見せる秒数(飛行の最初のうち)
MAX_FLIGHTS = 3       # 同時に出す飛行の上限(描く側で縦にずらして重ねる)
TAPE_LINES = 4        # 履歴テープに出す件数

# イベント → (飛ぶもの, 出発側)。飛ばないイベントはテープにだけ出す。
#   左 = Muse、右 = Claw'd(Claude)。post_out は Claude が Muse へ投稿
#   した記録なので、成功したときだけ手紙が右から左へ飛ぶ。
_FLIGHTS = {
    "post_out": ("letter", "clawd"),
    "mail_in": ("letter", "muse"),
    "ack": ("ack", "clawd"),
    "approval": ("ack", "clawd"),
}


def _epoch(record):
    if not isinstance(record, dict):
        return None
    value = record.get("epoch")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _key(record):
    """同じイベントを1つに数えるためのキー。since と tail の両方から
    読んだときに重なっても、二重に飛ばさない・テープに二重に出さない。"""
    return (record.get("ev"), _epoch(record),
            str(record.get("id") or record.get("head")
                or record.get("card") or ""))


def _unique_sorted(events):
    """壊れた記録を落とし、重複を除いて、新しい順に並べたレコード列。"""
    seen = set()
    out = []
    for record in events or []:
        if _epoch(record) is None or not isinstance(record.get("ev"), str):
            continue
        key = _key(record)
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    out.sort(key=_epoch, reverse=True)
    return out


def flights(events, now=None) -> list:
    """いま飛んでいる手紙・受領チェックの一覧(新しい順)。

    各要素は {item: "letter"|"ack", from: "muse"|"clawd", to: 反対側,
    progress: 0..1, press: bool}。飛ぶのは epoch から FLIGHT_SEC 秒の
    間だけ。未来のイベントや、飛び終わったイベントは出さない。
    press は approval の最初の PRESS_SEC 秒だけ True(Claw'd の
    押す動きの合図)。"""
    now = time.time() if now is None else now
    out = []
    for record in _unique_sorted(events):
        spec = _FLIGHTS.get(record["ev"])
        if spec is None:
            continue
        if record["ev"] == "post_out" and not record.get("ok"):
            continue   # 失敗した投稿は届いていないので飛ばない
        age = now - _epoch(record)
        if age < 0 or age >= FLIGHT_SEC:
            continue
        item, side = spec
        out.append({
            "item": item,
            "from": side,
            "to": "muse" if side == "clawd" else "clawd",
            "progress": age / FLIGHT_SEC,
            "press": record["ev"] == "approval" and age < PRESS_SEC,
        })
        if len(out) >= MAX_FLIGHTS:
            break
    return out


def flight_position(progress, from_side, left, right):
    """飛行の進み具合 → (x, 不透明度)。x はステージ左端からの座標で、
    left / right は両端(キャラの内側)。従来の fly と同じ形: 進み 0.85
    までは移動、残りは着いた場所で消えていく。"""
    p = min(1.0, max(0.0, progress))
    a, b = (left, right) if from_side == "muse" else (right, left)
    if p < 0.85:
        return (a + (b - a) * p / 0.85, 1.0)
    return (b, 1.0 - (p - 0.85) / 0.15)


# 状態(kind)の値をテープに出すときの日本語。生の英語名をそのまま出さない。
_KIND_JA = {"idle": "待機", "processing": "作業中", "responding": "返信中",
            "approval": "承認待ち", "no_reply": "返事なし", "unknown": "不明",
            "claude": "Claude確認中"}


def _summary(record) -> tuple:
    """テープの (種類, 要約)。要約は短く切る(描く側でも省略される)。"""
    ev = record["ev"]
    if ev == "post_out":
        return "投稿", f"→{record.get('to') or 'Muse'}"
    if ev == "mail_in":
        return "受信", str(record.get("note") or record.get("id") or "")
    if ev == "ack":
        return "受領", str(record.get("id") or "")
    if ev == "approval":
        return "承認", str(record.get("card") or "")
    if ev == "kind":
        kind = str(record.get("kind") or "")
        return "状態", _KIND_JA.get(kind, kind)
    if ev == "wake":
        return "起床", str(record.get("reason") or "")
    if ev == "monitor":
        return "見張り", str(record.get("phase") or "")
    for key in ("note", "head", "card", "id", "reason"):
        if record.get(key):
            return ev, str(record[key])
    return ev, ""


def tape_lines(events, now=None, count=TAPE_LINES) -> list:
    """履歴テープの行(新しい順に最大 count 件)。書式は「HH:MM:SS 種類 要約」。

    now は使わない(並びは epoch だけで決まる)が、flights と呼び方を
    揃えるために受ける。"""
    lines = []
    for record in _unique_sorted(events)[:count]:
        stamp = time.strftime("%H:%M:%S", time.localtime(_epoch(record)))
        label, summary = _summary(record)
        line = f"{stamp} {label} {summary}".rstrip()
        lines.append(line[:60])
    return lines
