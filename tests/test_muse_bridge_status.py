# test_muse_bridge_status.py
# Muse 見張りの状態をツールチップの行にする純関数(muse_bridge_status)のテスト。
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import muse_bridge_status as mbs  # noqa: E402

NOW = 1_800_000_000.0


def test_fmt_dur():
    assert mbs.fmt_dur(8) == "8秒"
    assert mbs.fmt_dur(252) == "4分12秒"
    assert mbs.fmt_dur(65) == "1分05秒"
    assert mbs.fmt_dur(3900) == "1時間5分"
    assert mbs.fmt_dur(-3) == "0秒"


def test_waiting_reply_with_busy_muse():
    data = {"state": "busy", "since": NOW - 65, "waiting_since": NOW - 252,
            "poll_sec": 15, "last_poll": NOW - 8, "approved": 2}
    assert mbs.describe(data, NOW) == [
        "Muse返事待ち 4分12秒（作業中 1分05秒）",
        "監視 15秒毎・次7秒・自動承認2件",
    ]


def test_waiting_only():
    data = {"state": "waiting", "since": NOW - 30, "waiting_since": NOW - 30,
            "poll_sec": 15, "last_poll": NOW}
    assert mbs.describe(data, NOW) == ["Muse返事待ち 30秒", "監視 15秒毎・次15秒"]


def test_settling_shows_count():
    data = {"state": "settling", "since": NOW - 20, "waiting_since": NOW - 100,
            "settle": 2, "settle_need": 4, "poll_sec": 15, "last_poll": NOW}
    assert mbs.describe(data, NOW)[0] == "Muse返事待ち 1分40秒（返事の確定待ち 20秒） 2/4"


def test_idle_without_waiting():
    data = {"state": "idle", "since": NOW - 600, "waiting_since": None,
            "poll_sec": 15, "last_poll": NOW - 3}
    assert mbs.describe(data, NOW)[0] == "Muse 待機中 10分00秒"


def test_stale_and_forgotten():
    data = {"state": "claude", "since": NOW - 200, "poll_sec": 15, "last_poll": NOW - 180}
    assert mbs.describe(data, NOW) == ["Muse監視 停止中（3分00秒前・最後はClaude確認中）"]
    data["last_poll"] = NOW - mbs.FORGET_SEC - 1
    assert mbs.describe(data, NOW) == []


def test_bad_input_is_quiet():
    assert mbs.describe(None, NOW) == []
    assert mbs.describe({}, NOW) == []
    assert mbs.describe({"state": "busy", "last_poll": "x"}, NOW) == []


def test_read():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        assert mbs.read(p) is None
        p.write_text("{broken", encoding="utf-8")
        assert mbs.read(p) is None
        p.write_text(json.dumps({"state": "idle"}), encoding="utf-8")
        assert mbs.read(p) == {"state": "idle"}


def test_panel_view_waiting():
    data = {"state": "waiting", "since": NOW - 30, "waiting_since": NOW - 252,
            "poll_sec": 15, "last_poll": NOW - 8, "approved": 2}
    view = mbs.panel_view(data, NOW)
    assert view["label"] == "返事待ち" and not view["stopped"]
    assert view["elapsed"] == 30 and view["waiting"] == 252
    assert view["approved"] == 2
    # 次の読み取りは 7 秒後(15 - 8)。割合は残り/間隔
    assert view["poll_next_sec"] == 7
    assert abs(view["poll_fraction"] - 7 / 15) < 1e-9
    assert view["settle_fraction"] is None and view["settle_text"] is None


def test_panel_view_settling():
    data = {"state": "settling", "since": NOW - 20, "waiting_since": NOW - 100,
            "settle": 2, "settle_need": 4, "poll_sec": 15, "last_poll": NOW}
    view = mbs.panel_view(data, NOW)
    assert view["settle_fraction"] == 0.5 and view["settle_text"] == "2/4"
    assert view["poll_fraction"] == 1.0   # 読んだばかりは満タン


def test_panel_view_poll_overdue_clamps():
    data = {"state": "busy", "since": NOW - 65, "waiting_since": None,
            "poll_sec": 15, "last_poll": NOW - 20}   # 次の読み取りを過ぎている
    view = mbs.panel_view(data, NOW)
    assert view["poll_fraction"] == 0.0 and view["poll_next_sec"] == 0
    assert view["waiting"] is None and view["approved"] == 0


def test_panel_view_stopped_and_forgotten():
    data = {"state": "claude", "since": NOW - 200, "poll_sec": 15,
            "last_poll": NOW - 180}
    view = mbs.panel_view(data, NOW)
    assert view["stopped"] and view["stale_sec"] == 180
    assert view["poll_fraction"] is None and view["label"] == "Claude確認中"
    data["last_poll"] = NOW - mbs.FORGET_SEC - 1
    assert mbs.panel_view(data, NOW) is None


def test_panel_view_bad_input_is_quiet():
    assert mbs.panel_view(None, NOW) is None
    assert mbs.panel_view({}, NOW) is None
    assert mbs.panel_view({"state": "busy", "last_poll": "x"}, NOW) is None


def _kd(kind, since_ago=10, header=None, **extra):
    d = {"kind": kind, "kind_since": NOW - since_ago, "header": header,
         "state": "idle", "since": NOW - 999, "waiting_since": None,
         "poll_sec": 15, "last_poll": NOW - 3}
    d.update(extra)
    return d


def test_kind_labels():
    expect = {"idle": "Muse 待機（指示待ち）", "responding": "Muse 応答中",
              "processing": "Muse 処理中", "approval": "Muse 承認待ち",
              "no_reply": "Muse 未応答", "unknown": "Muse 状態不明"}
    for kind, label in expect.items():
        view = mbs.panel_view(_kd(kind, 5), NOW)
        assert view["kind"] == kind and view["kind_label"] == label, kind
        assert view["kind_elapsed"] == 5
    # 知らない kind は unknown
    assert mbs.panel_view(_kd("zzz"), NOW)["kind_label"] == "Muse 状態不明"


def test_kind_processing_header():
    def lab(h):
        return mbs.panel_view(_kd("processing", header=h), NOW)["kind_label"]
    assert lab("実装中") == "Muse 処理中（実装中）"
    assert lab("リンク済み") == "Muse 処理中"
    assert lab("") == "Muse 処理中" and lab(None) == "Muse 処理中"
    assert lab("あ" * 20) == "Muse 処理中（" + "あ" * 12 + "…）"
    # processing 以外は見出しを添えない
    assert (mbs.panel_view(_kd("idle", header="実装中"), NOW)["kind_label"]
            == "Muse 待機（指示待ち）")


def test_kind_tone():
    def tone(kind, ago):
        return mbs.panel_view(_kd(kind, ago), NOW)["tone"]
    assert tone("idle", 9999) == "calm"
    assert tone("responding", 5) == "active" and tone("processing", 5000) == "active"
    assert tone("approval", 59) == "calm"
    assert tone("approval", 60) == "warn" and tone("approval", 179) == "warn"
    assert tone("approval", 180) == "alert"
    assert tone("no_reply", 59) == "warn" and tone("no_reply", 60) == "alert"
    assert tone("unknown", 59) == "calm" and tone("unknown", 60) == "alert"


def test_kind_describe_and_stopped():
    data = _kd("approval", 65)
    assert mbs.describe(data, NOW)[0] == "Muse 承認待ち 1分05秒"
    assert mbs.describe(data, NOW)[1] == "監視 15秒毎・次12秒"
    # 停止中は従来どおり(kind があっても停止の行。view は stopped)
    old = _kd("idle", last_poll=NOW - 180)
    assert mbs.describe(old, NOW)[0].startswith("Muse監視 停止中")
    assert mbs.panel_view(old, NOW)["stopped"]


def test_no_kind_is_backward_compatible():
    data = {"state": "busy", "since": NOW - 65, "waiting_since": NOW - 252,
            "poll_sec": 15, "last_poll": NOW - 8, "approved": 2}
    view = mbs.panel_view(data, NOW)
    assert view["kind"] is None and view["kind_label"] is None
    assert view["kind_elapsed"] is None and view["tone"] == "calm"
    assert view["label"] == "作業中" and view["elapsed"] == 65
    assert mbs.describe(data, NOW)[0] == "Muse返事待ち 4分12秒（作業中 1分05秒）"


def test_hold_last_good():
    good = {"kind": "idle", "last_poll": NOW}
    # 読めたらそれを使い、控えも更新する
    assert mbs.hold_last_good(good, None, None, NOW) == (good, good, NOW)
    # 読めなかったら直前の良いデータを10秒まで保持する
    assert mbs.hold_last_good(None, good, NOW, NOW + 3) == (good, good, NOW)
    assert mbs.hold_last_good(None, good, NOW, NOW + 10) == (good, good, NOW)
    # 10秒を超えたら None(控えは使わない)
    assert mbs.hold_last_good(None, good, NOW, NOW + 10.5)[0] is None
    # 控えが無ければ None。dict でないもの(壊れた読み)は更新しない
    assert mbs.hold_last_good(None, None, None, NOW)[0] is None
    assert mbs.hold_last_good([1], good, NOW, NOW + 1)[0] is good


def test_poll_sec_must_be_positive():
    for bad in (-15, 0):
        data = {"state": "idle", "since": NOW - 5, "last_poll": NOW - 3, "poll_sec": bad}
        view = mbs.panel_view(data, NOW)
        assert view["poll_fraction"] is None and view["poll_next_sec"] is None
        assert all("次" not in line for line in mbs.describe(data, NOW))


ALL_TESTS = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")


# ---- 自己申告(muse_self)の上乗せと食い違い ----

def _self(state="working", age=60, summary="作業の要約"):
    return {"state": state, "summary": summary, "at": NOW - age}


def test_self_report_fresh():
    rep = mbs.self_report({"muse_self": _self()}, NOW)
    assert rep["label"] == "作業中" and rep["summary"] == "作業の要約"
    assert rep["group"] == "active" and rep["age"] == 60


def test_self_report_stale_or_missing():
    assert mbs.self_report({"muse_self": _self(age=3601)}, NOW) is None
    assert mbs.self_report({"muse_self": _self(age=3600)}, NOW) is not None
    assert mbs.self_report({}, NOW) is None
    assert mbs.self_report({"muse_self": {"state": "working"}}, NOW) is None
    assert mbs.self_report({"muse_self": "working"}, NOW) is None


def test_self_report_timestamp_key_variants():
    for key in ("at", "received_at", "ts"):
        raw = {"state": "idle", "summary": "", key: NOW - 5}
        assert mbs.self_report({"muse_self": raw}, NOW)["label"] == "待機中"


def test_self_caution_disagreement():
    data = {"kind": "idle", "kind_since": NOW - 30,
            "muse_self": _self("working")}
    caution = mbs.self_caution(data, NOW)
    assert caution and "食い違っています" in caution


def test_self_caution_agreement_and_unjudgeable():
    base = {"kind_since": NOW - 30}
    assert mbs.self_caution({**base, "kind": "processing",
                             "muse_self": _self("working")}, NOW) is None
    assert mbs.self_caution({**base, "kind": "approval",
                             "muse_self": _self("waiting_approval")}, NOW) is None
    # 判定不能(no_reply / unknown)は食い違いにしない
    assert mbs.self_caution({**base, "kind": "no_reply",
                             "muse_self": _self("working")}, NOW) is None
    assert mbs.self_caution({**base, "kind": "unknown",
                             "muse_self": _self("idle")}, NOW) is None


def test_self_caution_quiet_working():
    data = {"kind": "processing", "kind_since": NOW - 700,
            "muse_self": _self("working", age=700)}
    caution = mbs.self_caution(data, NOW)
    assert caution and "更新されていません" in caution


def test_panel_view_carries_self_and_caution():
    data = {"state": "busy", "since": NOW - 65, "poll_sec": 15,
            "last_poll": NOW - 8, "kind": "idle", "kind_since": NOW - 30,
            "muse_self": _self("working")}
    v = mbs.panel_view(data, NOW)
    assert v["self_label"] == "作業中" and v["self_summary"] == "作業の要約"
    assert v["caution"] and "食い違っています" in v["caution"]
    v2 = mbs.panel_view({"state": "busy", "since": NOW - 65, "poll_sec": 15,
                         "last_poll": NOW - 8}, NOW)
    assert v2["self_label"] is None and v2["caution"] is None
