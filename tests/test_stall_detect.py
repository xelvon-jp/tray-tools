# tests/test_stall_detect.py
# 見張りの「宣言したのに動かない」検知(stall_detect.decide)を確かめる。
# 時刻は全部でたらめな epoch 秒で、実時間には依存しない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_stall_detect.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_claude_status.py と同じ形)。
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stall_detect as sd  # noqa: E402

T = 1_000_000.0
HEADING = "Tray-toolsの機能案を整理"


def msg(text, at):
    return {"text": text, "at": at}


def decide(messages, now, **kw):
    kw.setdefault("cards", 0)
    kw.setdefault("heading", HEADING)
    kw.setdefault("last_screen_change_at", None)
    return sd.decide(messages, now=now, **kw)


# ---------------------------------------------------------------------------
# 実際の止まり2件(2026-11-08)。どちらも「push します」と言ってターンが
# 終わり、カードも次の発言も無いまま見張りに指摘された。
# ---------------------------------------------------------------------------

def test_incident_test_env_fix_push_stall():
    messages = [
        msg("テストの環境依存の修正が終わりました。", T - 300),
        msg("push します。対象は tray-tools の branch `muse/test-env-fix`、"
            "変更は12ファイルです。", T),
    ]
    # 59秒ではまだ促さない
    d = decide(messages, T + 59, last_screen_change_at=T - 10)
    assert not d.nudge and d.reason == "waiting", d
    # 60秒たってもカードも新しい発言も無ければ促す
    d = decide(messages, T + 60, last_screen_change_at=T - 10)
    assert d.nudge and d.text == "宣言した操作を実行して", d
    assert d.reason == "declaration_stall" and d.matter, d


def test_incident_checklist_update_push_stall():
    messages = [
        msg("push します: tray-tools の `muse/checklist-update` へ "
            "docs/real-device-checklist.html の1ファイル"
            "（bridge-status の実機確認1件を追加、全21件）。", T),
    ]
    d = decide(messages, T + 120)
    assert d.nudge and d.text == "宣言した操作を実行して", d


# ---------------------------------------------------------------------------
# ルール(2): 【承認依頼します】のあとは90秒
# ---------------------------------------------------------------------------

def test_signal_needs_90_seconds():
    messages = [msg("【承認依頼します】", T)]
    d = decide(messages, T + 89)
    assert not d.nudge and d.reason == "waiting", d
    d = decide(messages, T + 90)
    assert d.nudge and d.reason == "signal_stall", d
    assert "承認カードが出ていません" in d.text, d


# ---------------------------------------------------------------------------
# 促さない条件
# ---------------------------------------------------------------------------

def test_card_present_no_nudge():
    messages = [msg("push します。", T)]
    d = decide(messages, T + 600, cards=1)
    assert not d.nudge and d.reason == "card_present", d


def test_moved_on_no_nudge():
    # 宣言のあとに別の発言があれば、話は続いている
    messages = [msg("push します。", T), msg("進捗: テストを実行中です。", T + 30)]
    d = decide(messages, T + 600)
    assert not d.nudge and d.reason == "no_declaration", d


def test_screen_changed_after_declaration_no_nudge():
    messages = [msg("push します。", T)]
    d = decide(messages, T + 600, last_screen_change_at=T + 120)
    assert not d.nudge and d.reason == "screen_active", d


def test_user_drafting_no_nudge():
    messages = [msg("push します。", T)]
    d = decide(messages, T + 600, user_drafting=True)
    assert not d.nudge and d.reason == "user_drafting", d


def test_muse_responding_no_nudge():
    messages = [msg("push します。", T)]
    d = decide(messages, T + 600, muse_responding=True)
    assert not d.nudge and d.reason == "muse_responding", d


def test_no_declaration_no_nudge():
    messages = [msg("テストは29件すべて通りました。", T)]
    d = decide(messages, T + 600)
    assert not d.nudge and d.reason == "no_declaration", d


def test_empty_messages_no_nudge():
    d = decide([], T + 600)
    assert not d.nudge and d.reason == "no_messages", d


# ---------------------------------------------------------------------------
# ルール(3): 同じ件は最大3回・間隔120秒以上
# ---------------------------------------------------------------------------

def test_nudge_cap_three():
    messages = [msg("push します。", T)]
    first = decide(messages, T + 60)
    past = [{"matter": first.matter, "at": T + 60},
            {"matter": first.matter, "at": T + 180},
            {"matter": first.matter, "at": T + 300}]
    d = decide(messages, T + 420, nudges=past)
    assert not d.nudge and d.reason == "capped", d


def test_nudge_interval_120():
    messages = [msg("push します。", T)]
    first = decide(messages, T + 60)
    past = [{"matter": first.matter, "at": T + 60}]
    d = decide(messages, T + 60 + 119, nudges=past)
    assert not d.nudge and d.reason == "interval", d
    d = decide(messages, T + 60 + 120, nudges=past)
    assert d.nudge, d


def test_other_matter_nudges_do_not_count():
    messages = [msg("push します。", T)]
    past = [{"matter": "別の見出し|push します。", "at": T + 30}] * 3
    d = decide(messages, T + 60, nudges=past)
    assert d.nudge, d


# ---------------------------------------------------------------------------
# ルール(5): 宣言の語彙は設定で足せる
# ---------------------------------------------------------------------------

def test_custom_vocabulary():
    messages = [msg("デプロイします。", T)]
    d = decide(messages, T + 60)
    assert not d.nudge, d  # 既定の語彙には無い
    config = sd.Config(declarations=sd.DEFAULT_DECLARATIONS + ("デプロイします",))
    d = decide(messages, T + 60, config=config)
    assert d.nudge, d


def test_korekara_declaration():
    messages = [msg("これから検証用のブランチを切って、差分を確認します。", T)]
    d = decide(messages, T + 60)
    assert d.nudge, d


# ---------------------------------------------------------------------------
# 状態タグの解析(2026-11-08 の取り決め)。文面は今日の実際の発言の形。
# ---------------------------------------------------------------------------

def test_tag_done_may_rest():
    v = sd.tag_verdict("作業が終わりました。結果は次のとおりです。\n【状態:完了】")
    assert v.tag == "完了" and v.may_rest and not v.should_nudge, v
    assert v.waiting_for is None, v


def test_tag_claude_waiting():
    v = sd.tag_verdict("取り込みはこちらでお願いします。\n【状態:Claude待ち】")
    assert v.tag == "Claude待ち" and v.may_rest and not v.should_nudge, v
    assert v.waiting_for == "claude", v


def test_tag_user_waiting():
    v = sd.tag_verdict("実機での確認をお願いします。\n【状態:陽太待ち】")
    assert v.tag == "陽太待ち" and v.may_rest and v.waiting_for == "user", v


def test_tag_working_should_nudge_when_stopped():
    v = sd.tag_verdict("検証を進めています。\n【状態:実行中】")
    assert v.tag == "実行中" and not v.may_rest and v.should_nudge, v


def test_tag_approval_should_nudge_when_stopped():
    v = sd.tag_verdict("push の準備ができました。\n【状態:承認依頼】")
    assert v.tag == "承認依頼" and not v.may_rest and v.should_nudge, v


def test_tag_legacy_signal_counts_as_approval():
    assert sd.parse_state_tag("【承認依頼します】") == "承認依頼"
    v = sd.tag_verdict("【承認依頼します】")
    assert v.should_nudge and not v.may_rest, v


def test_tag_missing():
    # 宣言止まりの実例(タグ導入前)は、タグとしては読めない。
    v = sd.tag_verdict("push します")
    assert v.tag is None and not v.may_rest and not v.should_nudge, v
    assert sd.parse_state_tag("") is None
    assert sd.parse_state_tag("  \n  ") is None


def test_tag_must_be_last_line_alone():
    # 最後の行より後ろに文があると、タグとして数えない。
    assert sd.parse_state_tag("【状態:完了】\nその後に一言") is None
    # 文中に埋めたタグも数えない(形の約束どおり、厳しめに倒す)。
    assert sd.parse_state_tag("終わりました【状態:完了】") is None


def test_tag_unknown_state():
    assert sd.parse_state_tag("【状態:不明】") is None


def test_tag_fullwidth_colon():
    assert sd.parse_state_tag("【状態：完了】") == "完了"


# ---------------------------------------------------------------------------
# 承認カードの「未表示の疑い」(改善案2)。題材は 2026-11-08 12:24 の実例:
# カードが PC に出ず、スマホだけに出た。
# ---------------------------------------------------------------------------

def test_absence_suspected_like_1224_case():
    # 承認依頼の申告から13分経っても、見張り側にカードが0件。
    v = sd.card_absence("waiting_approval", T, 0, T + 13 * 60)
    assert v.suspect and v.reason == "suspected_absent", v
    assert v.waiting_seconds == 13 * 60, v


def test_absence_within_generation_window():
    # 生成待ちの実測(213〜577秒)の範囲内なら、まだ疑わない。
    v = sd.card_absence("waiting_approval", T, 0, T + 300)
    assert not v.suspect and v.reason == "within_generation", v


def test_absence_card_present():
    v = sd.card_absence("waiting_approval", T, 1, T + 3600)
    assert not v.suspect and v.reason == "card_present", v


def test_absence_not_waiting():
    v = sd.card_absence("working", T, 0, T + 3600)
    assert not v.suspect and v.reason == "not_waiting", v


def test_absence_accepts_state_tag_word():
    # 状態タグの「承認依頼」でも同じ判定になる。
    v = sd.card_absence("承認依頼", T, 0, T + 601)
    assert v.suspect, v


def test_absence_no_report():
    v = sd.card_absence("waiting_approval", None, 0, T + 3600)
    assert not v.suspect and v.reason == "no_report", v


def test_absence_threshold_boundary():
    # ちょうど閾値(600秒)で疑い側に倒れる。
    v = sd.card_absence("waiting_approval", T, 0, T + 600)
    assert v.suspect, v


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
