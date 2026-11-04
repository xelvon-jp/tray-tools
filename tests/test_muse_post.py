# tests/test_muse_post.py
# muse_post の判断ロジックと進行を確かめる。Muse アプリが無くても通る。
#
# 純関数は muse_post_logic( UIA 依存なし)を、進行(cmd_post / cmd_wait)は
# 偽の Muse と一時リポジトリを使って確かめるので、Linux でも回る
# (20261105-01 #11〜#13)。UIA の実操作だけはここでは触らない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_muse_post.py
#   python3 tests/test_muse_post.py    (Linux でも可)
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

import muse_post as mpost  # noqa: E402
import muse_post_logic as mp  # noqa: E402  既存テストの mp. はこちらを指す

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


def test_new_chat_ready_needs_transition():
    # #1: Invoke 前と同じ画面の間は、空の作りたてでも「まだ切り替わっていない」
    before = _summary()
    sig = mp.view_signature(before)
    r = mp.check_new_chat_ready(_summary(), sig)
    assert r and "切り替わっていない" in r, r
    # 画面が変われば(子孫数が変わるなど)通る
    after = _summary(extra=41)
    assert mp.view_signature(after) != sig
    assert mp.check_new_chat_ready(after, sig) is None
    # before_sig を渡さなければ従来どおり状態だけで判定する
    assert mp.check_new_chat_ready(_summary()) is None


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


def test_before_invoke_busy():
    # #3: SetValue の待ちの間に作業中になったら、文面が一致していても押さない
    msg = "依頼です"
    r = mp.check_before_invoke(_summary("送信", "停止", value=msg), msg)
    assert r and "作業中" in r, r


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
        code = mpost.main(["post", "--message", "a\nb", "--dry-run"])
    assert code == 2, code
    assert "改行" in buf.getvalue()


def test_bad_args_exit_2_with_json():
    import io
    import json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            mpost.main(["post"])  # --message も --message-file も無い
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
        assert mpost.main(["--version"]) == 0
    assert re.fullmatch(r"\d{8}-\d{2}", json.loads(buf.getvalue())["version"])


def test_read_message_file():
    import tempfile
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "msg.txt"
        p.write_bytes("﻿依頼です\n".encode("utf-8"))  # BOM + 末尾の改行
        args = SimpleNamespace(message=None, message_file=str(p))
        assert mpost.read_message(args) == "依頼です"
        # 読めないファイルは main が終了コード 2 で返す
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mpost.main(["post", "--message-file", str(Path(d) / "none.txt")])
        assert code == 2, code


def test_logic_module_has_no_uia_imports():
    # #11: 判断モジュールが comtypes / copilot_loop を読んでいないことを見張る
    import ast
    tree = ast.parse((ROOT / "muse_post_logic.py").read_text(encoding="utf-8"))
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods.append(node.module or "")
    assert not any(m.startswith("comtypes") or m.startswith("copilot_loop") for m in mods), mods


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


def test_git_timeout_budget():
    # #8: 1回の git の制限は全体の残り時間と GIT_TIMEOUT の小さい方
    import time
    start = time.time()
    t = mpost._git_timeout(start, 30)
    assert t is not None and 0 < t <= 30
    assert mpost._git_timeout(start, 10 ** 9) == mp.GIT_TIMEOUT
    # 残りが尽きたら None(最初の1回だけは試す)
    assert mpost._git_timeout(start - 100, 10) is None
    assert mpost._git_timeout(start - 100, 10, first=True) == mp.GIT_TIMEOUT


# ---------------------------------------------------------------
# cmd_wait を一時リポジトリで確かめる(#13)
# ---------------------------------------------------------------
def _run_git(cwd, *argv):
    import subprocess
    proc = subprocess.run(["git", "-C", str(cwd), *argv],
                          capture_output=True, text=True)
    assert proc.returncode == 0, (argv, proc.stderr)
    return proc.stdout.strip()


def _make_wait_repos(d):
    """src(投稿先のつもり)に muse/done ブランチを作り、dst に clone する。"""
    src = Path(d) / "src"
    src.mkdir()
    _run_git(src, "init", "-b", "main")
    (src / "handoff").mkdir()
    (src / "handoff" / "result.md").write_text("結果\n", encoding="utf-8")
    _run_git(src, "add", ".")
    _run_git(src, "-c", "user.name=test", "-c", "user.email=test@example.com",
             "commit", "-m", "test")
    _run_git(src, "branch", "muse/done")
    sha = _run_git(src, "rev-parse", "muse/done")
    dst = Path(d) / "dst"
    import subprocess
    proc = subprocess.run(["git", "clone", "-q", str(src), str(dst)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return dst, sha


def test_wait_with_path_succeeds():
    import tempfile
    from types import SimpleNamespace
    if not _git_available():
        return
    with tempfile.TemporaryDirectory() as d:
        dst, sha = _make_wait_repos(d)
        args = SimpleNamespace(repo=str(dst), branch="muse/done",
                               path="handoff/result.md", timeout=30, interval=0.05)
        code, out = mpost.cmd_wait(args)
        assert code == 0 and out["ok"] is True, out
        # #9: 返す sha は fetch 後の先端(= ブランチの先端)と一致する
        assert out["sha"] == sha, out


def test_wait_without_path_reports_appeared_only():
    # #7: --path 無しでは「現れた」までしか言わない(ok にはしない)
    import tempfile
    from types import SimpleNamespace
    if not _git_available():
        return
    with tempfile.TemporaryDirectory() as d:
        dst, sha = _make_wait_repos(d)
        args = SimpleNamespace(repo=str(dst), branch="muse/done",
                               path=None, timeout=30, interval=0.05)
        code, out = mpost.cmd_wait(args)
        assert code == 1 and out["ok"] is False and out["appeared"] is True, out
        assert out["sha"] == sha


def test_wait_times_out_when_path_missing():
    import tempfile
    from types import SimpleNamespace
    if not _git_available():
        return
    with tempfile.TemporaryDirectory() as d:
        dst, _sha = _make_wait_repos(d)
        args = SimpleNamespace(repo=str(dst), branch="muse/done",
                               path="handoff/absent.md", timeout=0.6, interval=0.05)
        code, out = mpost.cmd_wait(args)
        assert code == 1 and out["reason"] == "timeout", out
        args2 = SimpleNamespace(repo=str(dst), branch="muse/absent",
                                path="handoff/result.md", timeout=0.6, interval=0.05)
        code2, out2 = mpost.cmd_wait(args2)
        assert code2 == 1 and out2["reason"] == "timeout", out2


def _git_available():
    import shutil
    return shutil.which("git") is not None


# ---------------------------------------------------------------
# cmd_post の進行を偽の Muse で確かめる(#12)
# ---------------------------------------------------------------
class _FakeMuse:
    """UIA の代わりの偽物。scan / poll / read_value / set_value / invoke だけ。

    poll は1回だけ scan して判定する(実物の繰り返し待ちはしない)。
    invoke('new') を受けたら after_new の要約に切り替わる。"""

    def __init__(self, value="", after_new=None, fail_poll_after_write=False):
        self.profile = mp.MUSE_PROFILE
        self.value = value
        self.after_new = after_new
        self.fail_poll_after_write = fail_poll_after_write
        self.switched = False
        self.set_calls = []
        self.invoke_calls = []
        self.closed = False

    def wait_awake(self):
        return True

    def _summary(self):
        s = {"descendants": 100, "inputs": 1, "input_value": self.value,
             "input_empty": mp.is_blank(self.value) if self.value is not None else None,
             "send": 1 if self.value else 0, "busy": False, "new_chat": 1,
             "open_list": 0, "message_buttons": 2, "chat_title": "話題"}
        if self.switched and self.after_new:
            s.update(self.after_new)
        return s

    def scan(self):
        refs = {"input": ["in"], "send": ["send"], "new_chat": ["new"], "open_list": [],
                "buttons": [], "texts": [], "items": []}
        return self._summary(), refs

    def poll(self, predicate, timeout):
        if self.fail_poll_after_write and self.set_calls:
            raise RuntimeError("scan が落ちた(偽)")
        s, refs = self.scan()
        return s, refs, bool(predicate(s))

    def read_value(self, el):
        return self.value

    def set_value(self, el, text):
        self.set_calls.append(text)
        self.value = text

    def invoke(self, el):
        self.invoke_calls.append(el)
        if el == "new":
            self.switched = True
        if el == "send":
            self.value = ""  # 送信されると投稿欄は空に戻る

    def close(self):
        self.closed = True


def _post_args(current_chat=True, dry_run=False):
    from types import SimpleNamespace
    return SimpleNamespace(current_chat=current_chat, dry_run=dry_run)


def test_cmd_post_current_chat_happy():
    fake = _FakeMuse()
    code, out = mpost.cmd_post(_post_args(), "依頼です", muse_factory=lambda: fake)
    assert code == 0 and out["ok"] is True, out
    assert fake.set_calls == ["依頼です"] and fake.invoke_calls == ["send"]
    assert fake.closed


def test_cmd_post_dry_run_touches_nothing():
    fake = _FakeMuse()
    code, out = mpost.cmd_post(_post_args(dry_run=True), "依頼です", muse_factory=lambda: fake)
    assert code == 0 and out["ok"] is True and out["plan"], out
    assert fake.set_calls == [] and fake.invoke_calls == []


def test_cmd_post_refuses_non_empty_input():
    fake = _FakeMuse(value="書きかけ")
    code, out = mpost.cmd_post(_post_args(), "依頼です", muse_factory=lambda: fake)
    assert code == 1 and "空でない" in out["reason"], out
    assert fake.set_calls == [] and fake.invoke_calls == []


def test_cmd_post_restores_on_exception_after_write():
    # #5: 書いた後に例外が起きたら、送らずに自分の文面だけを消して戻す
    fake = _FakeMuse(fail_poll_after_write=True)
    code, out = mpost.cmd_post(_post_args(), "依頼です", muse_factory=lambda: fake)
    assert code == 1 and out["ok"] is False, out
    assert fake.value == "", fake.set_calls
    assert any("空に戻した" in st for st in out["steps"]), out["steps"]


def test_cmd_post_new_chat_leftover_is_reported():
    # #6: 新規チャットの途中で止まったら、残ったものを理由に明示する
    after = {"descendants": 101, "message_buttons": 3, "chat_title": None}
    fake = _FakeMuse(after_new=after)
    code, out = mpost.cmd_post(_post_args(current_chat=False), "依頼です",
                               muse_factory=lambda: fake)
    assert code == 1, out
    assert "空になっていない" in out["reason"], out
    assert "空のサイドチャットが残っている" in out["reason"], out
    assert fake.set_calls == []  # 投稿はしていない


def test_restore_input_only_own_text():
    fake = _FakeMuse(value="依頼です")
    refs = {"input": ["in"]}
    assert mpost._restore_input(fake, refs, "依頼です") is True
    assert fake.value == ""
    # 人が打った分は消さない
    fake2 = _FakeMuse(value="人が打った分")
    assert mpost._restore_input(fake2, refs, "依頼です") is False
    assert fake2.value == "人が打った分"

# ---------------------------------------------------------------
# 承認カードの解析と判定(20261105-02。実機の実例がフィクスチャ)
# ---------------------------------------------------------------
G, D = 50026, 50007


def _card_items(files=("muse_post.py", "muse_post_logic.py"),
                action="Push files to GitHub",
                branch="muse/20261105-01-muse-post-fixes",
                owner="xelvon-jp", repo="tray-tools", extra_texts=()):
    items = [(G, "GitHubの承認リクエスト")]
    items += [(T, "Action"), (T, action),
              (T, "Branch or ref"), (T, branch),
              (T, "Repository owner"), (T, owner),
              (T, "Repository"), (T, repo)]
    for i, f in enumerate(files, 1):
        items += [(T, f"Files · {i} · File path"), (T, f),
                  (T, f"Files · {i} · Content"), (T, f"内容{i}の先頭")]
    items += [(T, "Additional request details are not shown."),
              (T, "プレビューは省略されています")]
    items += [(D, "Action"), (D, action)]  # DataItem の重複は無視される
    items += list(extra_texts)
    items += [(B, "許可する"), (B, "許可しない")]
    return items


def _expect(**kw):
    e = {"owner": "xelvon-jp", "repo": "tray-tools",
         "branch": "muse/20261105-01-muse-post-fixes",
         "allow_paths": ["muse_post.py", "muse_post_logic.py",
                         "tests/test_muse_post.py"]}
    e.update(kw)
    return e


def test_parse_approval_card():
    card = mp.parse_approval_card(_card_items())
    assert card["action"] == "Push files to GitHub"
    assert card["branch"] == "muse/20261105-01-muse-post-fixes"
    assert card["owner"] == "xelvon-jp" and card["repo"] == "tray-tools"
    assert card["files"] == ["muse_post.py", "muse_post_logic.py"]
    assert card["unknown"] == [] and card["duplicates"] == []
    assert card["approve_buttons"] == 1 and card["deny_buttons"] == 1
    assert card["files_complete"] is True
    # 窓全体の並びからでも、カードの外は切り捨てて1枚として読める
    items = [(T, "t0"), (B, "送信")] + _card_items() + [(B, "返信")]
    cards = mp.split_approval_cards(items)
    assert len(cards) == 1
    assert mp.parse_approval_card(cards[0])["files"] == card["files"]


def test_approval_decision_ok():
    card = mp.parse_approval_card(_card_items())
    assert mp.approval_decision(card, _expect()) is None


def test_approval_decision_refusals():
    # 許可されていないパス
    card = mp.parse_approval_card(_card_items(files=("muse_post.py", "settings.json")))
    r = mp.approval_decision(card, _expect())
    assert r and "許可されていないパス" in r, r
    # Action 違い
    card = mp.parse_approval_card(_card_items(action="Delete files from GitHub"))
    r = mp.approval_decision(card, _expect())
    assert r and "Action" in r, r
    # ファイル0件
    card = mp.parse_approval_card(_card_items(files=()))
    r = mp.approval_decision(card, _expect())
    assert r and "1件も無い" in r, r
    # 未知のラベル
    card = mp.parse_approval_card(_card_items(extra_texts=[(T, "謎のラベル"), (T, "値")]))
    r = mp.approval_decision(card, _expect())
    assert r and "未知" in r, r
    # ブランチ不一致と、muse/ で始まらないブランチ
    card = mp.parse_approval_card(_card_items(branch="muse/other"))
    r = mp.approval_decision(card, _expect())
    assert r and "一致しない" in r, r
    card = mp.parse_approval_card(_card_items(branch="main"))
    r = mp.approval_decision(card, _expect(branch="main"))
    assert r and "muse/" in r, r


# ---------------------------------------------------------------
# approve / watch を偽の Muse で確かめる
# ---------------------------------------------------------------
class _FakeApproveMuse:
    """承認カードのある窓の偽物。『許可する』を押されるとカードが消える。"""

    def __init__(self, card_items=None, banner=False, on_approve=None):
        self.profile = mp.MUSE_PROFILE
        self.card_items = card_items
        self.banner = banner
        self.on_approve = on_approve
        self.invoke_calls = []
        self.closed = False

    def wait_awake(self):
        return True

    def _items(self):
        items = [(T, f"t{i}") for i in range(40)] + [(E, "検索"), (E, "メッセージ")]
        if self.banner:
            items.append((B, "Muse、承認が必要です"))
        if self.card_items:
            items += self.card_items
        return items

    def scan(self):
        items = self._items()
        s = mp.summarize(items, [""])
        refs = {"input": ["in"], "send": [], "new_chat": [], "open_list": [],
                "buttons": [(n, n) for c, n in items if c == B],
                "texts": [n for c, n in items if c == T], "items": items}
        # 本物の scan と同じく、カードの Group 要素を cards に入れる(カードごとの切り出しは
        # card_contents が Group の子孫だけから行う)
        segments = mp.split_approval_cards(items)
        if segments:
            refs["cards"] = segments
        return s, refs

    def card_contents(self, segment):
        return segment, [(n, n) for c, n in segment if c == B]

    def poll(self, predicate, timeout):
        s, refs = self.scan()
        return s, refs, bool(predicate(s))

    def invoke(self, el):
        self.invoke_calls.append(el)
        if el == "許可する":
            self.card_items = None
            if self.on_approve:
                self.on_approve()

    def close(self):
        self.closed = True


def _approve_args(**kw):
    from types import SimpleNamespace
    a = {"owner": "xelvon-jp", "repo": "tray-tools",
         "branch": "muse/20261105-01-muse-post-fixes",
         "allow_path": ["muse_post.py", "muse_post_logic.py"], "dry_run": False}
    a.update(kw)
    return SimpleNamespace(**a)


def test_cmd_approve_happy_and_dry_run():
    fake = _FakeApproveMuse(card_items=_card_items())
    code, out = mpost.cmd_approve(_approve_args(), muse_factory=lambda: fake)
    assert code == 0 and out["approved"] is True, out
    assert fake.invoke_calls == ["許可する"]
    fake2 = _FakeApproveMuse(card_items=_card_items())
    code, out = mpost.cmd_approve(_approve_args(dry_run=True), muse_factory=lambda: fake2)
    assert code == 0 and out["would_approve"] is True, out
    assert fake2.invoke_calls == []


def test_cmd_approve_refusals_do_not_press():
    # 許可外のパス: 許可する も 許可しない も押さない
    fake = _FakeApproveMuse(card_items=_card_items(files=("settings.json",)))
    code, out = mpost.cmd_approve(_approve_args(), muse_factory=lambda: fake)
    assert code == 1 and "許可されていないパス" in out["reason"], out
    assert fake.invoke_calls == []
    # カード2枚
    fake = _FakeApproveMuse(card_items=_card_items() + _card_items())
    code, out = mpost.cmd_approve(_approve_args(), muse_factory=lambda: fake)
    assert code == 1 and "2 枚" in out["reason"], out
    assert fake.invoke_calls == []
    # main ブランチは引数の時点で拒否
    fake = _FakeApproveMuse(card_items=_card_items(branch="main"))
    code, out = mpost.cmd_approve(_approve_args(branch="main"), muse_factory=lambda: fake)
    assert code == 2, out
    assert fake.invoke_calls == []
    # カードが無い
    fake = _FakeApproveMuse()
    code, out = mpost.cmd_approve(_approve_args(), muse_factory=lambda: fake)
    assert code == 1 and "見つからない" in out["reason"], out


def _make_repos_no_result(d):
    """結果ファイルの無い muse/done ブランチを持つ src と、その clone。"""
    src = Path(d) / "src2"
    src.mkdir()
    _run_git(src, "init", "-b", "main")
    (src / "README.md").write_text("x\n", encoding="utf-8")
    _run_git(src, "add", ".")
    _run_git(src, "-c", "user.name=test", "-c", "user.email=test@example.com",
             "commit", "-m", "test")
    _run_git(src, "branch", "muse/done")
    dst = Path(d) / "dst2"
    import subprocess
    proc = subprocess.run(["git", "clone", "-q", str(src), str(dst)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return src, dst


def _watch_args(dst, **kw):
    from types import SimpleNamespace
    a = {"repo_dir": str(dst), "owner": "xelvon-jp", "repo": "tray-tools",
         "branch": "muse/done", "path": "handoff/result.md",
         "allow_path": ["muse_post.py", "muse_post_logic.py"],
         "timeout": 30, "interval": 0.05}
    a.update(kw)
    return SimpleNamespace(**a)


def test_watch_done_without_card():
    import tempfile
    if not _git_available():
        return
    with tempfile.TemporaryDirectory() as d:
        dst, _sha = _make_wait_repos(d)  # muse/done に結果ファイルがある
        fake = _FakeApproveMuse()
        code, out = mpost.cmd_watch(_watch_args(dst), muse_factory=lambda: fake)
        assert code == 0 and out["status"] == "done" and out["approved"] == [], out


def test_watch_needs_human_on_refused_card():
    import tempfile
    if not _git_available():
        return
    with tempfile.TemporaryDirectory() as d:
        _src, dst = _make_repos_no_result(d)
        fake = _FakeApproveMuse(card_items=_card_items(
            branch="muse/done", files=("settings.json",)))
        code, out = mpost.cmd_watch(_watch_args(dst), muse_factory=lambda: fake)
        assert code == 1 and out["status"] == "needs_human", out
        assert fake.invoke_calls == []


def test_watch_needs_human_on_banner_only():
    import tempfile
    if not _git_available():
        return
    with tempfile.TemporaryDirectory() as d:
        _src, dst = _make_repos_no_result(d)
        fake = _FakeApproveMuse(banner=True)
        code, out = mpost.cmd_watch(_watch_args(dst), muse_factory=lambda: fake)
        assert code == 1 and out["status"] == "needs_human", out
        assert "見えない" in out["reason"], out


def test_watch_approves_then_done():
    import tempfile
    if not _git_available():
        return
    with tempfile.TemporaryDirectory() as d:
        src, dst = _make_repos_no_result(d)

        def _deliver_result():
            _run_git(src, "checkout", "muse/done")
            (src / "handoff").mkdir(exist_ok=True)
            (src / "handoff" / "result.md").write_text("結果\n", encoding="utf-8")
            _run_git(src, "add", ".")
            _run_git(src, "-c", "user.name=test", "-c", "user.email=test@example.com",
                     "commit", "-m", "result")
            _run_git(src, "checkout", "main")

        fake = _FakeApproveMuse(
            card_items=_card_items(branch="muse/done"), on_approve=_deliver_result)
        code, out = mpost.cmd_watch(_watch_args(dst), muse_factory=lambda: fake)
        assert code == 0 and out["status"] == "done", out
        assert len(out["approved"]) == 1, out
        assert out["approved"][0]["files"] == ["muse_post.py", "muse_post_logic.py"]
        assert fake.invoke_calls == ["許可する"]


# ---------------------------------------------------------------
# post --chat(既存チャットへの投稿)
# ---------------------------------------------------------------
def test_match_chat_buttons():
    names = ["対象チャット", "対象チャット 未読のチャット",
             "対象チャット 未読のアップデート", "対象チャット2", "別"]
    assert mp.match_chat_buttons(names, "対象チャット") == names[:3]
    assert mp.match_chat_buttons(names, "無い名前") == []


class _FakeChatMuse(_FakeMuse):
    """一覧→チャット切り替えの偽物。stage で画面が変わる。"""

    def __init__(self, target="対象チャット", with_expect=True):
        super().__init__()
        self.target = target
        self.with_expect = with_expect
        self.stage = "closed"

    def scan(self):
        s, refs = super().scan()
        if self.stage == "closed":
            s["open_list"] = 1
            s["chat_title"] = "旧話題"
            refs["open_list"] = ["openlist"]
        elif self.stage == "list":
            s["open_list"] = 0
            s["chat_title"] = None
            refs["buttons"] = [(self.target + " 未読のチャット", "chatbtn")]
        else:
            s["open_list"] = 1
            s["chat_title"] = self.target
            refs["texts"] = (["前の依頼 20261105-02 の続き"] if self.with_expect
                             else ["関係ない話"])
        return s, refs

    def invoke(self, el):
        if el == "openlist":
            self.invoke_calls.append(el)
            self.stage = "list"
            return
        if el == "chatbtn":
            self.invoke_calls.append(el)
            self.stage = "chat"
            return
        super().invoke(el)


def _chat_args(**kw):
    from types import SimpleNamespace
    a = {"current_chat": False, "dry_run": False,
         "chat": "対象チャット", "expect_text": "20261105-02"}
    a.update(kw)
    return SimpleNamespace(**a)


def _with_fast_timeouts(fn):
    old = (mpost.OPEN_LIST_TIMEOUT, mpost.CHAT_SWITCH_TIMEOUT, mpost.POLL_SEC)
    mpost.OPEN_LIST_TIMEOUT = mpost.CHAT_SWITCH_TIMEOUT = 0.3
    mpost.POLL_SEC = 0.01
    try:
        fn()
    finally:
        mpost.OPEN_LIST_TIMEOUT, mpost.CHAT_SWITCH_TIMEOUT, mpost.POLL_SEC = old


def test_cmd_post_chat_happy():
    def run():
        fake = _FakeChatMuse()
        code, out = mpost.cmd_post(_chat_args(), "続きの依頼です",
                                   muse_factory=lambda: fake)
        assert code == 0 and out["ok"] is True, out
        assert fake.invoke_calls == ["openlist", "chatbtn", "send"], fake.invoke_calls
        assert fake.set_calls == ["続きの依頼です"]
    _with_fast_timeouts(run)


def test_cmd_post_chat_expect_text_missing():
    def run():
        fake = _FakeChatMuse(with_expect=False)
        code, out = mpost.cmd_post(_chat_args(), "続きの依頼です",
                                   muse_factory=lambda: fake)
        assert code == 1 and "見つからない" in out["reason"], out
        assert fake.set_calls == []  # 投稿していない
    _with_fast_timeouts(run)


def test_cmd_post_chat_not_found():
    def run():
        fake = _FakeChatMuse(target="別のチャット")
        code, out = mpost.cmd_post(_chat_args(), "続きの依頼です",
                                   muse_factory=lambda: fake)
        assert code == 1 and "見つからない" in out["reason"], out
        assert fake.set_calls == []
    _with_fast_timeouts(run)


def test_parse_card_real_quirks():
    """実機(2026-11-05)のカードに出た3つ: 方向制御文字入りの問いかけ・空の Text・
    番号の付かない Content(コミットメッセージ)。どれも未知扱いにしない。"""
    items = [(G, "GitHubの承認リクエスト"),
             (T, "Museに⁨GitHubアカウントでこのアクションを実行⁩を許可しますか？"),
             (T, ""),
             (T, "Action"), (T, "Push files to GitHub"),
             (T, "Branch or ref"), (T, "muse/x"),
             (T, "Repository owner"), (T, "xelvon-jp"),
             (T, "Repository"), (T, "tray-tools"),
             (T, "Files · 1 · File path"), (T, "README.md"),
             (T, "Files · 1 · Content"), (T, "# 中身"),
             (T, "Content"), (T, "README に節を足す"),
             (B, "許可する"), (B, "許可しない")]
    card = mp.parse_approval_card(items)
    assert card["unknown"] == [], card["unknown"]
    assert card["commit_message"] == "README に節を足す"
    expect = {"owner": "xelvon-jp", "repo": "tray-tools", "branch": "muse/x",
              "allow_paths": ["README.md"]}
    assert mp.approval_decision(card, expect) is None


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
