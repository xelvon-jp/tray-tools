# test_bridge_events_view.py
# 連携ステージの「実イベントで飛ぶ手紙」と「履歴テープ」の材料(bridge_events_view)
# のテスト。Qt に依存しない純関数なので、標準ライブラリだけで確かめる。
#
#   python tests/test_bridge_events_view.py
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge_events_view as bev  # noqa: E402

NOW = 1_700_000_000.0   # 固定の「いま」。進み具合を決定的に確かめるため


def _ev(ev, age, **fields):
    rec = {"ev": ev, "epoch": NOW - age}
    rec.update(fields)
    return rec


def test_flights_directions():
    # 投稿(Claude→Muse・成功) → 手紙が Claw'd(右)から Muse(左)へ
    [f] = bev.flights([_ev("post_out", 0.5, to="Muse", ok=True)], NOW)
    assert f["item"] == "letter" and f["from"] == "clawd" and f["to"] == "muse"
    assert abs(f["progress"] - 0.25) < 1e-9
    # 投稿の失敗は飛ばない
    assert bev.flights([_ev("post_out", 0.5, ok=False)], NOW) == []
    # 郵便受けに届いた → 手紙が Muse(左)から Claw'd(右)へ
    [f] = bev.flights([_ev("mail_in", 1.0, id="mb-1")], NOW)
    assert f["item"] == "letter" and f["from"] == "muse" and f["to"] == "clawd"
    assert abs(f["progress"] - 0.5) < 1e-9
    # ack → 受領チェックが Claw'd から Muse へ
    [f] = bev.flights([_ev("ack", 0.2, id="mb-1")], NOW)
    assert f["item"] == "ack" and f["from"] == "clawd" and f["press"] is False
    # 承認 → チェック + 最初のうちだけ「押す動き」(press)
    [f] = bev.flights([_ev("approval", 0.2, card="push")], NOW)
    assert f["item"] == "ack" and f["press"] is True
    [f] = bev.flights([_ev("approval", 1.5, card="push")], NOW)
    assert f["press"] is False
    # 状態変化・見張り・起床は飛ばない(テープだけ)
    others = [_ev("kind", 0.1, kind="processing"), _ev("monitor", 0.1),
              _ev("wake", 0.1, reason="mailbox")]
    assert bev.flights(others, NOW) == []


def test_flights_age_window():
    # 飛び終わったイベントは出さない。未来のイベントも出さない
    assert bev.flights([_ev("mail_in", bev.FLIGHT_SEC)], NOW) == []
    assert bev.flights([_ev("mail_in", bev.FLIGHT_SEC + 30)], NOW) == []
    assert bev.flights([_ev("mail_in", -1.0)], NOW) == []
    # 生まれたては progress 0
    [f] = bev.flights([_ev("mail_in", 0.0)], NOW)
    assert f["progress"] == 0.0


def test_flights_dedup_and_cap():
    # 同じイベントが重なって読まれても1回だけ飛ぶ(同じ ev・epoch・id)
    rec = _ev("ack", 0.3, id="mb-9")
    assert len(bev.flights([rec, dict(rec), dict(rec)], NOW)) == 1
    # id が違えば別のイベントとして飛ぶ
    two = [_ev("ack", 0.3, id="mb-9"), _ev("ack", 0.3, id="mb-10")]
    assert len(bev.flights(two, NOW)) == 2
    # 同時にたくさん来ても上限まで。新しものが残る
    many = [_ev("mail_in", 0.1 * i, id=f"mb-{i}") for i in range(8)]
    got = bev.flights(many, NOW)
    assert len(got) == bev.MAX_FLIGHTS
    assert got[0]["progress"] <= got[-1]["progress"]   # 新しい順(進みが浅い順)


def test_flights_broken_records():
    assert bev.flights([{"ev": "mail_in"}, {"epoch": NOW}, {}, None,
                        _ev("mail_in", 0.1)], NOW) != []
    assert bev.flights([], NOW) == [] and bev.flights(None, NOW) == []


def test_flight_position():
    left, right = 20.0, 200.0
    # muse(左)発: 左端から出て、0.85 で右端に着き、残りで消える
    assert bev.flight_position(0.0, "muse", left, right) == (left, 1.0)
    x, a = bev.flight_position(0.85, "muse", left, right)
    assert abs(x - right) < 1e-9 and a == 1.0
    x, a = bev.flight_position(0.925, "muse", left, right)
    assert x == right and abs(a - 0.5) < 1e-9
    # clawd(右)発は逆向き
    assert bev.flight_position(0.0, "clawd", left, right) == (right, 1.0)
    x, _a = bev.flight_position(0.85, "clawd", left, right)
    assert abs(x - left) < 1e-9
    for p in (0.0, 0.3, 0.7, 0.99):
        x, a = bev.flight_position(p, "muse", left, right)
        assert left <= x <= right and 0.0 <= a <= 1.0


def test_tape_lines():
    events = [
        _ev("post_out", 300, to="Muse", ok=True, head="x"),
        _ev("mail_in", 200, id="mb-20261108-02", note="確認"),
        _ev("ack", 100, id="mb-20261108-02"),
        _ev("approval", 50, card="push files"),
        _ev("kind", 10, kind="processing"),
        _ev("wake", 5, reason="mailbox"),
    ]
    lines = bev.tape_lines(events, NOW)
    assert len(lines) == bev.TAPE_LINES == 4
    # 新しい順。時刻は HH:MM:SS
    assert lines[0].endswith("起床 mailbox"), lines
    assert "状態 processing" in lines[1]
    assert "承認 push files" in lines[2]
    assert "ack mb-20261108-02" in lines[3]
    for line in lines:
        stamp = line.split(" ", 1)[0]
        hh, mm, ss = stamp.split(":")
        assert len(hh) == len(mm) == len(ss) == 2
    # 投稿は行き先つき、受信は note(無ければ id)
    lines = bev.tape_lines(events, NOW, count=6)
    assert "投稿 →Muse" in lines[5], lines
    assert "受信 確認" in lines[4]
    # 壊れた記録は飛ばす
    assert bev.tape_lines([{"ev": "ack"}, None, {}], NOW) == []
    assert bev.tape_lines([], NOW) == [] and bev.tape_lines(None, NOW) == []


def test_coop_view_carries_flights_and_tape():
    import coop_view as cv

    now = time.time()
    muse = {"last_poll": now - 1, "poll_sec": 15, "state": "idle",
            "kind": "processing", "kind_since": now - 30,
            "cards": 0, "mailbox_fail": 0, "header": "実装中"}
    events = [{"ev": "mail_in", "epoch": now - 0.5, "id": "mb-1"},
              {"ev": "approval", "epoch": now - 400, "card": "push"}]
    view = cv.coop_view(muse, [], now, events=events)
    assert view is not None and len(view["flights"]) == 1
    assert view["flights"][0]["item"] == "letter"
    assert len(view["tape"]) == 2 and "承認 push" in view["tape"][1]
    # イベントを渡さなければ空(従来の呼び出し方を壊さない)
    view = cv.coop_view(muse, [], now)
    assert view["flights"] == [] and view["tape"] == []
    # 状態表現(parked)は従来どおり item に残る
    muse2 = dict(muse, kind="approval", kind_since=now - 5, cards=1)
    view = cv.coop_view(muse2, [], now, events=events)
    assert view["item"] == "parked"


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
