# tests/test_muse_post.py
# muse_post.py の判断ロジック(純関数)を確かめる。Muse アプリが無くても通る。
# UIA には触らない(Muse の窓を探しもしない)。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_muse_post.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_claude_status.py と同じ形)。
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import muse_post as mp  # noqa: E402

B, E, T = mp.CONTROL_BUTTON, mp.CONTROL_EDIT, 50020
OPEN = "チャットとサイドチャットを開く"


def _items(*buttons, inputs=1, extra=40):
    """要約に渡す木を組み立てる。extra は数合わせの Text(起きている木に見せる)。"""
    items = [(T, f"t{i}") for i in range(extra)]
    items += [(E, "検索")]
    items += [(E, "メッセージ")] * inputs
    items += [(B, b) for b in buttons]
    return items


def _summary(*buttons, value="", inputs=1, extra=40):
    values = [value] * inputs
    return mp.summarize(_items(*buttons, inputs=inputs, extra=extra), values)


# ---------------------------------------------------------------
# チャット名の抽出
# ---------------------------------------------------------------
def test_chat_title_with_unread():
    names = ["送信", f"Tray-tools ルール調査の試運転 未読のチャット {OPEN}"]
    assert mp.chat_title_from_names(names) == "Tray-tools ルール調査の試運転"


def test_chat_title_without_unread():
    assert mp.chat_title_from_names([f"Claude と Muse の連携仕様 {OPEN}"]) == "Claude と Muse の連携仕様"


def test_chat_title_no_button():
    assert mp.chat_title_from_names(["送信", "停止", "返信"]) is None
    assert mp.chat_title_from_names([]) is None
    # 名前部分が空なら不明扱い
    assert mp.chat_title_from_names([OPEN]) is None
    assert mp.chat_title_from_names([f"未読のチャット {OPEN}"]) is None


def test_summary_counts_and_title():
    s = _summary("停止", "返信", "リアクションする", f"話題A {OPEN}", value="")
    assert s["busy"] is True and s["message_buttons"] == 2
    assert s["open_list"] == 1 and s["chat_title"] == "話題A"
    assert s["inputs"] == 1 and s["input_empty"] is True
    # 検索欄は投稿欄に数えない
    assert _summary(inputs=0)["inputs"] == 0
    # 公開用の要約に下書きの中身は出ない
    pub = mp.public_summary(_summary(value="書きかけ"))
    assert "input_value" not in pub and pub["input_length"] == 4


# ---------------------------------------------------------------
# 空チャット判定
# ---------------------------------------------------------------
def test_new_chat_empty():
    assert mp.is_new_chat_empty(_summary("送信"))
    assert not mp.is_new_chat_empty(_summary("返信"))
    assert not mp.is_new_chat_empty(_summary("リアクションする", "リアクションする"))
    assert mp.check_new_chat_ready(_summary()) is None
    assert "空になっていない" in mp.check_new_chat_ready(_summary("返信"))
    assert "作業中" in mp.check_new_chat_ready(_summary("停止"))


# ---------------------------------------------------------------
# 安全確認
# ---------------------------------------------------------------
def test_blank():
    assert mp.is_blank("") and mp.is_blank("  ") and mp.is_blank("​")
    assert not mp.is_blank(None) and not mp.is_blank("a")


def test_initial_refuses_non_empty_input():
    for current in (True, False):
        r = mp.check_initial(_summary("新しいサイドチャット", value="書きかけ"), current)
        assert r and "空でない" in r, r


def test_initial_refuses_missing_or_ambiguous_input():
    assert "見つからない" in mp.check_initial(_summary(inputs=0), True)
    assert "2 個" in mp.check_initial(_summary(inputs=2), True)
    # 値が読めない(ValuePattern 無し)は空とみなさない
    s = mp.summarize(_items(), [None])
    assert s["input_empty"] is None
    assert "読めない" in mp.check_initial(s, True)
    # 木が起きていない
    assert "起きていない" in mp.check_initial(_summary(extra=0), True)


def test_initial_busy_handling():
    # 表示中のチャットへは、作業中なら投稿しない
    assert "作業中" in mp.check_initial(_summary("停止"), current_chat=True)
    # 新しいチャットを作るなら、表示中が作業中でも着手してよい(作ってから再確認)
    assert mp.check_initial(_summary("停止", "新しいサイドチャット"), current_chat=False) is None
    assert mp.check_initial(_summary("停止", f"x {OPEN}"), current_chat=False) is None


def test_initial_new_chat_needs_a_way():
    r = mp.check_initial(_summary(), current_chat=False)
    assert r and "作れない" in r
    assert mp.check_initial(_summary(f"x {OPEN}"), current_chat=False) is None
    assert "2 個" in mp.check_initial(
        _summary("新しいサイドチャット", "新しいサイドチャット"), current_chat=False)


def test_before_set():
    assert mp.check_before_set(_summary()) is None
    assert "作業中" in mp.check_before_set(_summary("停止"))
    assert "空でない" in mp.check_before_set(_summary(value="x"))


def test_before_invoke():
    msg = "依頼です"
    assert mp.check_before_invoke(_summary("送信", value=msg), msg) is None
    # 送信ボタンが無い
    assert "出ない" in mp.check_before_invoke(_summary(value=msg), msg)
    # 2つあったら押さない
    assert "2 個" in mp.check_before_invoke(_summary("送信", "送信", value=msg), msg)
    # 読み戻しが一致しない(取りこぼし・人の打鍵が混ざった)
    assert "一致しない" in mp.check_before_invoke(_summary("送信", value=msg + "x"), msg)
    assert "一致しない" in mp.check_before_invoke(_summary("送信", value=""), msg)
    assert "見失った" in mp.check_before_invoke(_summary("送信", value=msg, inputs=0), msg)


def test_title_settled():
    assert mp.title_settled(None, "話題", new_chat=True)
    assert not mp.title_settled("話題", "話題", new_chat=True)  # まだ名前が付いていない
    assert mp.title_settled("仮", "本当の名前", new_chat=True)
    assert not mp.title_settled(None, "新しいチャット", True, ["新しいチャット"])
    assert not mp.title_settled(None, None, new_chat=False)
    assert mp.title_settled("話題", "話題", new_chat=False)
    # 投稿直後は投稿文そのものが名前になっている(実測)。それはまだ決まっていない
    assert not mp.title_settled(None, "依頼です", True, message="依頼です")
    assert mp.title_settled(None, "依頼の要約", True, message="依頼です")


# ---------------------------------------------------------------
# 引数
# ---------------------------------------------------------------
def test_multiline_message_refused():
    assert mp.message_error("1行", False) is None
    assert "改行" in mp.message_error("1行目\n2行目", False)
    assert "改行" in mp.message_error("1行目\r2行目", False)
    assert mp.message_error("1行目\n2行目", True) is None
    assert "空" in mp.message_error("   ", False)


def test_multiline_exits_2():
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mp.main(["post", "--message", "a\nb", "--dry-run"])
    assert code == 2, code
    assert "改行" in buf.getvalue()


def test_bad_args_exit_2_with_json():
    import io
    import json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            mp.main(["post"])  # --message も --message-file も無い
        raise AssertionError("SystemExit にならなかった")
    except SystemExit as e:
        assert e.code == 2
    assert json.loads(buf.getvalue())["ok"] is False


def test_version():
    import io
    import json
    import re
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert mp.main(["--version"]) == 0
    assert re.fullmatch(r"\d{8}-\d{2}", json.loads(buf.getvalue())["version"])


# ---------------------------------------------------------------
# wait の ls-remote 出力
# ---------------------------------------------------------------
def test_parse_ls_remote():
    sha = "69975350044168e0ea8f92bc0a9b9da78cdda48b"
    other = "f4ff59b0000000000000000000000000000000aa"
    out = (f"{other}\trefs/heads/foo/muse/x\n"
           f"{sha}\trefs/heads/muse/x\n")
    assert mp.parse_ls_remote(out, "muse/x") == sha
    # 末尾一致で混ざる別ブランチは採らない
    assert mp.parse_ls_remote(f"{other}\trefs/heads/foo/muse/x\n", "muse/x") is None
    assert mp.parse_ls_remote("", "muse/x") is None
    assert mp.parse_ls_remote("warning: something\n", "muse/x") is None


def test_redact_hides_urls():
    msg = "fatal: unable to access 'https://user:tok@github.com/o/r.git/': 403"
    r = mp.redact(msg)
    assert "tok" not in r and "github.com" not in r and "<url>" in r
    assert "github.com" not in mp.redact("git@github.com:o/r.git: denied")


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
