# test_coop_view.py
# 連携ステージの判定(coop_view)のテスト。Qt 不要の純関数なので素のまま回る。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coop_view as cv

NOW = 1_000_000.0


def muse(**kw):
    base = {"last_poll": NOW - 2, "poll_sec": 15, "state": "idle", "kind": "idle",
            "kind_since": NOW - 300, "cards": 0, "mailbox_fail": 0}
    base.update(kw)
    return base


def view(m, entries=(), now=NOW):
    return cv.coop_view(m, list(entries), now)


def test_none_cases():
    assert view(None) is None
    assert view({"kind": "idle"}) is None                    # last_poll が無い
    assert view(muse(last_poll=NOW - 6 * 3600 - 1)) is None  # 古すぎる
    assert view(muse(last_poll=NOW - 6 * 3600)) is not None  # 境界は出す
    assert view(muse(last_poll=NOW - 3600)) is not None      # 止まっていても出す


def test_stopped_wins():
    v = view(muse(last_poll=NOW - 91, kind="approval", mailbox_fail=20))
    assert v["tone"] == "alert" and v["banner_title"] == "見張りが止まっています"
    assert v["banner_sub"] == "最後の確認 1分31秒前"
    assert v["cards"][2] == ("見張り", "停止")
    # 90秒ちょうどはまだ止まっていない
    assert view(muse(last_poll=NOW - 90))["banner_title"] != "見張りが止まっています"


def test_mailbox_fail():
    v = view(muse(mailbox_fail=10, kind="approval", kind_since=NOW - 5))
    assert v["banner_title"] == "郵便受けにつながりません" and v["tone"] == "alert"
    v = view(muse(mailbox_fail=9, kind="idle"))
    assert v["banner_title"] == "全員待機中"
    assert v["cards"][1] == ("郵便受け", "取得失敗") and v["cards"][2] == ("見張り", "不調")


def test_approval_card_missing():
    v = view(muse(kind="approval", cards=0, kind_since=NOW - 20))
    assert v["banner_title"] == "承認カードが見えません" and v["tone"] == "alert"
    assert v["banner_sub"] == "見張りが確認しています"
    v = view(muse(kind="approval", cards=0, kind_since=NOW - 19))
    assert v["banner_title"] == "Muse が承認待ち" and v["tone"] == "approval"
    # カードがあれば20秒を超えても承認待ち
    v = view(muse(kind="approval", cards=1, kind_since=NOW - 100))
    assert v["banner_title"] == "Muse が承認待ち" and v["tone"] == "approval"
    assert v["banner_sub"] == "Claw'd が見張って押します"
    assert v["muse_anim"] == "approval" and v["clawd_anim"] == "wait"
    assert v["item"] == "parked" and v["props"] == set()
    assert v["banner_time"] == "1分40秒"
    assert v["cards"][0] == ("承認", "1分40秒")   # 承認待ちの間は経過秒
    # cards が無い(古いファイル)なら見えない扱いにしない
    m = muse(kind="approval", kind_since=NOW - 100)
    del m["cards"]
    assert view(m)["banner_title"] == "Muse が承認待ち"


def test_approval_turns_alert_at_180():
    assert view(muse(kind="approval", cards=1, kind_since=NOW - 179))["tone"] == "approval"
    v = view(muse(kind="approval", cards=1, kind_since=NOW - 180))
    assert v["tone"] == "alert" and v["banner_title"] == "Muse が承認待ち"
    assert v["muse_anim"] == "alert" and v["item"] == "wobble"
    assert v["props"] == {"bang", "sweat"}


def test_no_reply_and_unknown():
    assert view(muse(kind="no_reply", kind_since=NOW - 59))["banner_title"] == "全員待機中"
    v = view(muse(kind="no_reply", kind_since=NOW - 60))
    assert v["banner_title"] == "Muse が返事をしていません" and v["tone"] == "alert"
    assert view(muse(kind="unknown", kind_since=NOW - 59))["banner_title"] == "全員待機中"
    v = view(muse(kind="unknown", kind_since=NOW - 60))
    assert v["banner_title"] == "状態が分かりません" and v["tone"] == "alert"


def test_muse_working():
    for kind in ("processing", "responding"):
        v = view(muse(kind=kind, kind_since=NOW - 80, header="実装中"))
        assert v["tone"] == "work" and v["banner_title"] == "いま：Muse が作業中"
        assert v["banner_sub"] == "実装中" and v["banner_time"] == "1分20秒"
        assert v["muse_anim"] == "work" and v["clawd_anim"] == "watch"
        assert v["item"] == "fly" and v["props"] == {"spark"}
    for header in (None, "", "リンク済み", "  "):
        assert view(muse(kind="processing", header=header))["banner_sub"] \
            == "Claw'd が見守っています"
    # Muse 作業中は Claude の行より優先
    v = view(muse(kind="processing"), [{"state": "ask", "asked": NOW - 5}])
    assert v["banner_title"] == "いま：Muse が作業中"


def test_claude_ask_and_running():
    v = view(muse(), [{"state": "ask", "asked": NOW - 30}, {"state": "running"}])
    assert v["tone"] == "approval" and v["banner_title"] == "Claude が承認待ち"
    assert v["banner_sub"] == "陽太さんの許可待ちです" and v["banner_time"] == "30秒"
    v = view(muse(), [{"state": "running", "started": NOW - 125}])
    assert v["tone"] == "claude" and v["banner_title"] == "いま：Claude が作業中"
    assert v["banner_sub"] == "Claw'd が手を動かしています"
    assert v["muse_anim"] == "claude" and v["clawd_anim"] == "work"
    assert v["item"] is None and v["props"] == {"dots"}
    v = view(muse(), [{"state": "running", "phase": "tool",
                       "tool_name": "mcp__tray-tools__capture"}])
    assert v["banner_sub"] == "ツール実行中（capture）"
    v = view(muse(), [{"state": "running", "phase": "tool", "tool_name": "Bash"}])
    assert v["banner_sub"] == "ツール実行中（Bash）"
    v = view(muse(), [{"state": "running", "phase": "tool"}])
    assert v["banner_sub"] == "Claw'd が手を動かしています"
    # 完了した行だけなら待機
    assert view(muse(), [{"state": "done"}])["banner_title"] == "全員待機中"


def test_done_window():
    m = muse(prev_kind="processing", kind="idle", kind_since=NOW - 5)
    v = view(m)
    assert v["tone"] == "done" and v["banner_title"] == "完了しました"
    assert v["banner_sub"] == "Muse の作業が終わりました"
    assert v["muse_anim"] == "done" and v["clawd_anim"] == "happy"
    assert v["item"] == "back" and v["props"] == {"hat", "confetti"}
    assert view(dict(m, kind_since=NOW - 6))["tone"] == "sleep"      # 6秒で終わり
    for prev in ("responding", "approval"):
        assert view(dict(m, prev_kind=prev))["tone"] == "done"
    assert view(dict(m, prev_kind="idle"))["tone"] == "sleep"
    # Claude が動いていれば Claude が勝つ(10 より 9 が上)
    assert view(m, [{"state": "running"}])["tone"] == "claude"


def test_sleep_default():
    v = view(muse())
    assert v["tone"] == "sleep" and v["banner_title"] == "全員待機中"
    assert v["banner_sub"] == "陽太さんの指示待ちです"
    assert v["muse_anim"] == "sleep" and v["clawd_anim"] == "sleep"
    assert v["item"] is None and v["props"] == {"zz"}
    assert len(v["cards"]) == 3


def test_cards():
    v = view(muse(last_approval_sec=3.04))
    assert v["cards"][0] == ("承認", "3.0秒")
    assert view(muse(approved=4))["cards"][0] == ("承認", "4件")
    assert view(muse(approved=0))["cards"][0] == ("承認", "—")
    assert view(muse(last_approval_sec=1.5, approved=4))["cards"][0] == ("承認", "1.5秒")
    assert view(muse(last_mail_at=NOW - 59))["cards"][1] == ("郵便受け", "受信 59秒前")
    assert view(muse(last_mail_at=NOW - 60))["cards"][1] == ("郵便受け", "受信 60秒前")
    assert view(muse(last_mail_at=NOW - 61))["cards"][1] == ("郵便受け", "空")
    assert view(muse())["cards"][1] == ("郵便受け", "空")
    assert view(muse(mailbox_fail=1, last_mail_at=NOW - 1))["cards"][1] == ("郵便受け", "取得失敗")
    assert view(muse())["cards"][2] == ("見張り", "正常")


def test_state_claude_overrides_stale_kind():
    # (a) 見張りが Claude を起こして終了: kind が approval / cards==0 のまま残っていても異常にしない
    m = muse(state="claude", since=NOW - 30, kind="approval", cards=0, kind_since=NOW - 120)
    v = view(m)
    assert v["tone"] == "claude" and v["banner_title"] == "Claude が確認中"
    assert v["banner_sub"] == "Muse の見張りが起こしました" and v["banner_time"] == "30秒"
    assert v["muse_anim"] == "claude" and v["clawd_anim"] == "work"
    assert v["item"] is None and v["props"] == {"dots"} and len(v["cards"]) == 3
    # since が無ければ kind_since からの経過
    m2 = dict(m)
    del m2["since"]
    assert view(m2)["banner_time"] == "2分00秒"
    # mailbox_fail など 2) 以降より先
    assert view(dict(m, mailbox_fail=20))["tone"] == "claude"
    # (b) stale が先
    v = view(dict(m, last_poll=NOW - 91))
    assert v["tone"] == "alert" and v["banner_title"] == "見張りが止まっています"
    # (c) state が claude 以外なら従来どおり
    v = view(dict(m, state="Museは指示待ち"))
    assert v["banner_title"] == "承認カードが見えません" and v["tone"] == "alert"


ALL_TESTS = [test_none_cases, test_stopped_wins, test_mailbox_fail,
             test_approval_card_missing, test_approval_turns_alert_at_180,
             test_no_reply_and_unknown, test_muse_working, test_claude_ask_and_running,
             test_done_window, test_sleep_default, test_cards,
             test_state_claude_overrides_stale_kind]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
