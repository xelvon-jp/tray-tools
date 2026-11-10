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


def test_all_idle_elapsed_counts_from_both_idle():
    # Muse は 13 分前から待機だが、Claude が 30 秒前に終わった → 全員待機の経過は 30 秒
    v = view(muse(kind="idle", kind_since=NOW - 13 * 60), [{"state": "done", "updated": NOW - 30}])
    assert v["banner_title"] == "全員待機中" and v["banner_time"] == "30秒"
    # Claude の記録が無ければ、これまでどおり Muse の待機時間
    v = view(muse(kind="idle", kind_since=NOW - 13 * 60))
    assert v["banner_title"] == "全員待機中" and v["banner_time"] != "30秒"


def test_mailbox_fail():
    v = view(muse(mailbox_fail=10, kind="approval", kind_since=NOW - 5))
    assert v["banner_title"] == "郵便受けにつながりません" and v["tone"] == "alert"
    v = view(muse(mailbox_fail=9, kind="idle"))
    assert v["banner_title"] == "全員待機中"
    assert v["cards"][1] == ("郵便受け", "取得失敗") and v["cards"][2] == ("見張り", "不調")


def test_route_label():
    # 経路は郵便受けカードの値の頭に出る(バナーのサブ行には出さない)
    v = view(muse(route="home"))
    assert v["cards"][1] == ("郵便受け", "家・空")
    assert "経路" not in v["banner_sub"]
    v = view(muse(route="worker"))
    assert v["cards"][1] == ("郵便受け", "私書箱・空")
    v = view(muse(route="home", mailbox_fail=10, kind="approval", kind_since=NOW - 5))
    assert v["cards"][1] == ("郵便受け", "家・取得失敗")
    v = view(muse())
    assert v["cards"][1] == ("郵便受け", "空")


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


def test_kind_without_kind_since_falls_back():
    # kind だけで kind_since が無い: since → waiting_since の順で経過を出す
    m = muse(kind="approval", cards=0, since=NOW - 30)
    del m["kind_since"]
    v = view(m)
    assert v["banner_title"] == "承認カードが見えません" and v["tone"] == "alert"
    assert v["banner_time"] == "30秒"
    m = muse(kind="approval", cards=0, waiting_since=NOW - 25)
    del m["kind_since"]
    assert view(m)["banner_title"] == "承認カードが見えません"
    assert view(m)["banner_time"] == "25秒"
    # どれも無ければ経過 0 とみなして警告は出さない
    m = muse(kind="approval", cards=0)
    del m["kind_since"]
    assert view(m)["banner_title"] == "Muse が承認待ち"
    # 承認待ち 180 秒の赤も since から数える
    m = muse(kind="approval", cards=1, since=NOW - 200)
    del m["kind_since"]
    assert view(m)["tone"] == "alert"


def test_negative_last_approval_sec_is_clamped():
    assert view(muse(last_approval_sec=-2.5))["cards"][0] == ("承認", "0.0秒")


def test_all_idle_flag():
    v = view(muse())
    assert v["all_idle"] is True and v["paused"] is False
    # Muse が作業中・Claude が実行中/承認待ちなら、全員待機ではない
    assert view(muse(kind="processing"))["all_idle"] is False
    assert view(muse(), [{"state": "running", "started": NOW - 10}])["all_idle"] is False
    assert view(muse(), [{"state": "ask", "asked": NOW - 10}])["all_idle"] is False
    # 完了の直後(完了表示の間)も待機とは言わない
    done = muse(kind="idle", prev_kind="processing", kind_since=NOW - 2)
    assert view(done)["all_idle"] is False


def test_paused_view():
    v = cv.coop_view(muse(), [], NOW, paused=True)
    assert v["banner_title"] == "見張り休止中" and v["tone"] == "sleep"
    assert v["banner_sub"] == "ブリッジのボタンで再開できます"
    assert v["banner_time"] == "" and v["all_idle"] is False and v["paused"] is True
    assert v["cards"][2] == ("見張り", "休止")
    # 停止(last_poll が古い)より優先する: 休止中は異常と言わない
    v2 = cv.coop_view(muse(last_poll=NOW - 300), [], NOW, paused=True)
    assert v2["banner_title"] == "見張り休止中"


def test_playbook_display():
    desk_run = {"state": "running", "playbook": "pb4006",
                "request_id": "req-t1", "started_at": NOW - 30}
    # Muse の作業中より優先して出す
    v = cv.coop_view(muse(kind="processing"), [], NOW, desk=desk_run)
    assert v["banner_title"] == "プレイブック(PB-4006)実行中" and v["tone"] == "work"
    assert v["banner_sub"] == "常駐の再起動（req-t1）"
    assert v["banner_time"] == "30秒"
    # 移行期間は旧名の合図も同じ表示にする
    v = cv.coop_view(muse(kind="processing"), [], NOW,
                     desk=dict(desk_run, playbook="traytools_restart"))
    assert v["banner_title"] == "プレイブック(PB-4006)実行中"
    assert v["banner_sub"] == "常駐の再起動（req-t1）"
    # 承認待ちには勝たない
    v = cv.coop_view(muse(kind="approval", kind_since=NOW - 5), [], NOW, desk=desk_run)
    assert v["banner_title"] == "Muse が承認待ち"
    # 完了 (60秒以内) は done で出す
    desk_done = {"state": "done", "playbook": "pb4006", "ok": True,
                 "summary": "再起動しました", "started_at": NOW - 50,
                 "finished_at": NOW - 20}
    v = cv.coop_view(muse(), [], NOW, desk=desk_done)
    assert v["banner_title"] == "プレイブック(PB-4006)完了" and v["tone"] == "done"
    assert v["banner_sub"] == "常駐の再起動: 再起動しました"
    # 完了から60秒を超えたら出さない
    v = cv.coop_view(muse(), [], NOW, desk=dict(desk_done, finished_at=NOW - 61))
    assert v["banner_title"] == "全員待機中"
    # 失敗は alert
    v = cv.coop_view(muse(), [], NOW, desk=dict(desk_done, ok=False, finished_at=NOW - 5))
    assert v["banner_title"] == "プレイブック(PB-4006)失敗" and v["tone"] == "alert"
    # 実行中でも10分を超えたら古い合図として無視する
    v = cv.coop_view(muse(kind="processing"), [], NOW,
                     desk=dict(desk_run, started_at=NOW - 601))
    assert v["banner_title"] != "プレイブック(PB-4006)実行中"


def test_auto_approval_display():
    import time as _time
    m = muse(last_approval_at=NOW - 30, last_approval_preview="mb-20261110-99")
    v = cv.coop_view(m, [], NOW)
    assert v["banner_title"] == "自動承認しました" and v["tone"] == "done"
    assert v["banner_sub"] == "予告 mb-20261110-99"
    hhmm = _time.strftime("%H:%M", _time.localtime(NOW - 30))
    assert v["cards"][0] == ("承認", f"自動 {hhmm}")
    # 承認待ちには勝たない
    v = cv.coop_view(muse(kind="approval", kind_since=NOW - 5, cards=1,
                          last_approval_at=NOW - 30), [], NOW)
    assert v["banner_title"] == "Muse が承認待ち"
    # バナーは90秒だけ。カードの「自動 HH:MM」は30分残る
    v = cv.coop_view(muse(last_approval_at=NOW - 91, approved=3), [], NOW)
    assert v["banner_title"] == "全員待機中"
    assert v["cards"][0][1].startswith("自動 ")
    v = cv.coop_view(muse(last_approval_at=NOW - 1801, approved=3), [], NOW)
    assert v["cards"][0] == ("承認", "3件")


ALL_TESTS = [test_none_cases, test_stopped_wins, test_mailbox_fail, test_route_label,
             test_playbook_display, test_auto_approval_display,
             test_approval_card_missing, test_approval_turns_alert_at_180,
             test_no_reply_and_unknown, test_muse_working, test_claude_ask_and_running,
             test_done_window, test_sleep_default, test_cards,
             test_kind_without_kind_since_falls_back, test_negative_last_approval_sec_is_clamped,
             test_state_claude_overrides_stale_kind, test_all_idle_flag, test_paused_view,
             test_all_idle_elapsed_counts_from_both_idle]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")


def test_self_overlay_and_caution():
    # 自己申告が無ければ self / caution は空(従来の表示と変わらない)
    v = view(muse())
    assert v["self"] is None and v["caution"] is None
    # 申告=作業中・画面判定も処理中なら、上乗せだけ(注意なし)
    m = muse(kind="processing",
             muse_self={"state": "working", "summary": "実装中", "at": NOW - 30})
    v = view(m)
    assert v["self"]["label"] == "作業中" and v["caution"] is None
    # 申告=作業中なのに画面は待機 → 食い違いの注意
    m2 = muse(kind="idle",
              muse_self={"state": "working", "summary": "実装中", "at": NOW - 30})
    v2 = view(m2)
    assert v2["caution"] and "食い違っています" in v2["caution"]
