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
