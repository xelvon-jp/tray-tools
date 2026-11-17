# tests/test_copilot_io.py
# Copilot への共通入口(copilot_io.py)を、fake の transport で確かめる。
# UIA には触らない(窓が無い環境でも、送る/待つ/取るの流れと、
# 失敗が「利用者への文」になることを見る)。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_copilot_io.py
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import copilot_io  # noqa: E402


class FakeTransport:
    """copilot_loop.Copilot の代わり。fail_at で落とす場所を指定できる。"""

    def __init__(self, *, send_ok=True, idle=True, response="応答の本文",
                 blocks=("print(1)",), snapshot=100, fail_at=None,
                 input_value=None):
        self._send_ok = send_ok
        self._idle = idle
        self._response = response
        self._blocks = list(blocks)
        self._snapshot = snapshot
        self._fail_at = fail_at
        self._input_value = input_value
        self.sent = []
        self.new_response_args = None
        self.closed = False

    def _maybe_fail(self, where):
        if self._fail_at == where:
            raise RuntimeError("Copilot の窓が見つかりません")

    def snapshot_length(self):
        self._maybe_fail("snapshot")
        return self._snapshot

    def input_text(self):
        self._maybe_fail("input")
        return self._input_value

    def send_prompt(self, text):
        self._maybe_fail("send")
        self.sent.append(text)
        return self._send_ok

    def wait_until_idle(self, timeout):
        self._maybe_fail("wait")
        return self._idle, 1.5

    def new_response(self, previous_length, sent_prompt=None):
        self._maybe_fail("response")
        self.new_response_args = (previous_length, sent_prompt)
        return self._response

    def code_blocks(self):
        self._maybe_fail("blocks")
        return list(self._blocks)

    def close(self):
        self.closed = True


def test_send_text_ok():
    fake = FakeTransport()
    result = copilot_io.send_text("質問です", transport=fake)
    assert result.ok and result.message
    assert fake.sent == ["質問です"]
    assert fake.closed  # one-shot は閉じる


def test_send_text_no_window():
    fake = FakeTransport(fail_at="snapshot")
    result = copilot_io.send_text("質問です", transport=fake)
    assert not result.ok
    assert "窓が見つかりません" in result.message  # 理由が文に残る


def test_send_text_send_button_missing():
    fake = FakeTransport(send_ok=False)
    result = copilot_io.send_text("質問です", transport=fake)
    assert not result.ok and "送信できませんでした" in result.message


def test_send_empty_text():
    fake = FakeTransport()
    result = copilot_io.send_text("   ", transport=fake)
    assert not result.ok
    assert fake.sent == []  # transport には触らない


def test_session_flow():
    fake = FakeTransport(snapshot=4321, response="答え", blocks=["x = 1"])
    session = copilot_io.CopilotSession(fake)
    assert session.send("聞きたいこと").ok
    waited = session.wait(timeout=10)
    assert waited.ok and waited.elapsed == 1.5
    response = session.last_response()
    assert response.ok and response.text == "答え"
    assert response.code_blocks == ["x = 1"]
    # 送信直前の全文長と、送った本文が取る側へ渡っていること
    assert fake.new_response_args == (4321, "聞きたいこと")
    session.close()
    assert fake.closed


def test_wait_timeout():
    fake = FakeTransport(idle=False)
    session = copilot_io.CopilotSession(fake)
    assert session.send("質問").ok
    waited = session.wait(timeout=5)
    assert not waited.ok and "終わりませんでした" in waited.message


def test_last_response_without_send():
    """送信していないセッションでも、全文から最後の応答を取る形に倒す。"""
    fake = FakeTransport()
    session = copilot_io.CopilotSession(fake)
    response = session.last_response()
    assert response.ok and response.text == "応答の本文"
    assert fake.new_response_args == (0, None)


def test_last_response_blocks_unavailable():
    """コードブロックが取れないプロファイルでも、本文だけは返す。"""
    fake = FakeTransport(fail_at="blocks")
    session = copilot_io.CopilotSession(fake)
    assert session.send("質問").ok
    response = session.last_response()
    assert response.ok and response.text == "応答の本文"
    assert response.code_blocks == []


def test_response_failure_is_message():
    fake = FakeTransport(fail_at="response")
    session = copilot_io.CopilotSession(fake)
    assert session.send("質問").ok
    response = session.last_response()
    assert not response.ok and "取り出せませんでした" in response.message


def test_send_blocked_by_existing_input():
    """入力欄に書きかけの文があるときは、送らずに確認待ちで返す。"""
    fake = FakeTransport(input_value="書きかけの文")
    result = copilot_io.send_text("質問です", transport=fake)
    assert not result.ok and result.needs_confirm
    assert "書きかけ" in result.message
    assert fake.sent == []  # まだ送っていない


def test_send_forced_despite_existing_input():
    """確認が取れたあとは force=True で送り直せる。"""
    fake = FakeTransport(input_value="書きかけの文")
    result = copilot_io.send_text("質問です", transport=fake, force=True)
    assert result.ok and not result.needs_confirm
    assert fake.sent == ["質問です"]


def test_send_ignores_blank_input():
    """入力欄が空白だけなら、書きかけとはみなさない。"""
    fake = FakeTransport(input_value="  \n ")
    result = copilot_io.send_text("質問です", transport=fake)
    assert result.ok and fake.sent == ["質問です"]


def test_loop_transport_input_unreadable_fails_closed():
    """入力欄が読めないときは、空と誤認して送らず失敗で止める。

    CopilotLoopTransport.input_text は、read_input が None(入力欄が
    見つからない)を返したら例外にする。セッションはそれを失敗の
    結果に畳み、send_prompt は呼ばれない。"""
    transport = copilot_io.CopilotLoopTransport()

    class FakeCp:
        def __init__(self):
            self.sent = []

        def snapshot_length(self):
            return 100

        def read_input(self):
            return None  # 入力欄が見つからない = 読めない

        def send_prompt(self, text):
            self.sent.append(text)
            return True

        def close(self):
            pass

    fake_cp = FakeCp()
    transport._cp = fake_cp
    try:
        transport.input_text()
    except RuntimeError as e:
        assert "入力欄" in str(e)
    else:
        raise AssertionError("RuntimeError になるはず")
    result = copilot_io.CopilotSession(transport).send("質問です")
    assert not result.ok and "入力欄" in result.message
    assert fake_cp.sent == []  # 送っていない


# ---------------------------------------------------------------------------
# SubprocessTransport(fake の裏方実行役)
# ---------------------------------------------------------------------------
class FakeRunner:
    """裏方プロセスの代わり。op ごとに決まった応答を返す。"""

    def __init__(self, *, input_value=None, error=None, raise_error=None):
        self._input_value = input_value
        self._error = error
        self._raise_error = raise_error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request["op"])
        if self._raise_error is not None:
            raise self._raise_error
        if self._error is not None:
            return {"ok": False, "error": self._error}
        op = request["op"]
        if op == "snapshot":
            return {"ok": True, "length": 777}
        if op == "input":
            return {"ok": True, "input_text": self._input_value}
        if op == "send":
            return {"ok": True, "sent": True}
        if op == "send_escape":
            return {"ok": True, "status": "sent"}
        if op == "wait":
            return {"ok": True, "done": True, "elapsed": 2.0}
        if op == "wait_sendable":
            return {"ok": True, "done": True, "elapsed": 0.0}
        if op == "press_match":
            return {"ok": True, "pressed": True,
                    "forced_ratio": 0.95
                    if "force_threshold" in request else None}
        if op == "response":
            assert request["previous_length"] == 777
            return {"ok": True, "text": "裏方からの応答"}
        if op == "blocks":
            return {"ok": True, "blocks": ["y = 2"]}
        raise AssertionError(f"知らない op: {op}")


def test_subprocess_session_flow():
    runner = FakeRunner()
    session = copilot_io.CopilotSession(copilot_io.SubprocessTransport(runner))
    assert session.send("聞きたいこと").ok
    waited = session.wait(timeout=10)
    assert waited.ok and waited.elapsed == 2.0
    response = session.last_response()
    assert response.ok and response.text == "裏方からの応答"
    assert response.code_blocks == ["y = 2"]
    # 操作は全部裏方へ流れ、順番も往復のとおり
    assert runner.requests == ["snapshot", "wait_sendable", "input", "send",
                               "wait", "response", "blocks"]


def test_send_escape_via_subprocess():
    """ESC の standalone 操作が裏方へ流れ、状態語が文に残る
    (1-67)。近道 (モジュール関数) でも同じ。"""
    runner = FakeRunner()
    transport = copilot_io.SubprocessTransport(runner)
    result = copilot_io.CopilotSession(transport).send_escape()
    assert result.ok and result.message == "ESC送信: sent"
    assert runner.requests == ["send_escape"]
    shortcut = copilot_io.send_escape(
        transport=copilot_io.SubprocessTransport(runner))
    assert shortcut.ok and shortcut.message == "ESC送信: sent"
    assert runner.requests == ["send_escape", "send_escape"]


def test_send_escape_status_not_sent_is_failure():
    """状態語が sent でなければ失敗として返る (1-67)。
    transport が操作を持たなければ、その旨の失敗になる。"""
    fake = FakeTransport()  # send_escape を持たない
    result = copilot_io.CopilotSession(fake).send_escape()
    assert not result.ok and "ESC送信" in result.message


def test_send_escape_via_loop_transport():
    """CopilotLoopTransport は Copilot.send_escape の状態語を
    そのまま返す (1-67)。"""
    transport = copilot_io.CopilotLoopTransport()

    class FakeCp:
        def send_escape(self):
            return "no-hwnd"

        def close(self):
            pass

    transport._cp = FakeCp()
    assert transport.send_escape() == "no-hwnd"
    result = copilot_io.CopilotSession(transport).send_escape()
    assert not result.ok and result.message == "ESC送信: no-hwnd"


def test_subprocess_no_window():
    runner = FakeRunner(error="Copilot の窓が見つかりません")
    result = copilot_io.send_text(
        "質問です", transport=copilot_io.SubprocessTransport(runner))
    assert not result.ok
    assert "窓が見つかりません" in result.message


def test_subprocess_input_occupied_needs_confirm():
    runner = FakeRunner(input_value="書きかけの文")
    transport = copilot_io.SubprocessTransport(runner)
    result = copilot_io.send_text("質問です", transport=transport)
    assert not result.ok and result.needs_confirm
    assert "send" not in runner.requests  # 裏方でも送っていない
    forced = copilot_io.send_text("質問です", transport=transport, force=True)
    assert forced.ok
    assert runner.requests.count("send") == 1


def test_subprocess_worker_timeout_is_failure_result():
    runner = FakeRunner(raise_error=copilot_io.WorkerError(
        "Copilot の裏方が 90 秒以内に終わりませんでした"))
    result = copilot_io.send_text(
        "質問です", transport=copilot_io.SubprocessTransport(runner))
    assert not result.ok
    assert "裏方" in result.message and "終わりませんでした" in result.message


# ---------------------------------------------------------------------------
# run_worker(本物のサブプロセス。UIA は叩かない小さなスクリプトで確かめる)
# ---------------------------------------------------------------------------
def _script_argv(code):
    return [sys.executable, "-c", code]


def test_run_worker_ok():
    argv = _script_argv(
        "import json; print(json.dumps({'ok': True, 'length': 42}))")
    response = copilot_io.run_worker({"op": "snapshot"}, timeout=30, argv=argv)
    assert response == {"ok": True, "length": 42}


def test_run_worker_garbage_is_error():
    argv = _script_argv("print('JSON ではない')")
    try:
        copilot_io.run_worker({"op": "snapshot"}, timeout=30, argv=argv)
    except copilot_io.WorkerError as e:
        assert "異常終了" in str(e)
    else:
        raise AssertionError("WorkerError になるはず")


def test_run_worker_timeout_is_error():
    argv = _script_argv("import time; time.sleep(30)")
    try:
        copilot_io.run_worker({"op": "snapshot"}, timeout=1, argv=argv)
    except copilot_io.WorkerError as e:
        assert "終わりませんでした" in str(e)
    else:
        raise AssertionError("WorkerError になるはず")


def test_worker_script_fails_cleanly_without_uia():
    """本物の裏方を起こしても、UIA の無い環境では例外でなく
    失敗の結果が返る(窓が無い実機でも同じ形になる)。"""
    session = copilot_io.CopilotSession(
        copilot_io.SubprocessTransport(op_timeout=60))
    result = session.send("質問です")
    assert not result.ok and result.message
    session.close()


class FillTransport(FakeTransport):
    """fill_prompt / probe_input を持つ fake(CopilotLoopTransport の形)。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.filled = []

    def fill_prompt(self, text):
        self.filled.append(text)
        return True

    def probe_input(self):
        return [{"automation_id": "userInput", "control_type": "Edit",
                 "name": ""}]


def test_fill_writes_without_sending():
    transport = FillTransport()
    result = copilot_io.fill_text("テストです", transport=transport)
    assert result.ok and "送信はしていません" in result.message
    assert transport.filled == ["テストです"]
    assert transport.sent == []  # 送信の段は踏んでいない


def test_fill_refuses_nonempty_draft():
    # 入力欄に書きかけがあるときは、テストでも上書きしない。
    transport = FillTransport(input_value="書きかけ")
    result = copilot_io.fill_text("テストです", transport=transport)
    assert not result.ok and result.needs_confirm
    assert transport.filled == []


def test_fill_force_overwrites():
    transport = FillTransport(input_value="書きかけ")
    result = copilot_io.fill_text(
        "テストです", transport=transport, force=True)
    assert result.ok and transport.filled == ["テストです"]


def test_fill_without_fill_support_is_a_clean_failure():
    result = copilot_io.fill_text("テストです", transport=FakeTransport())
    assert not result.ok and not result.needs_confirm


def test_probe_returns_candidates():
    result = copilot_io.probe_input(transport=FillTransport())
    assert result.ok
    assert result.candidates[0]["automation_id"] == "userInput"


def test_probe_without_candidates_fails_cleanly():
    class EmptyTransport(FillTransport):
        def probe_input(self):
            return []

    result = copilot_io.probe_input(transport=EmptyTransport())
    assert not result.ok and result.candidates == []


def test_subprocess_transport_passes_profile():
    recorded = []
    runner = FakeRunner()

    def spy(request, timeout):
        recorded.append(dict(request))
        return runner(request, timeout)

    session = copilot_io.CopilotSession(copilot_io.SubprocessTransport(
        spy, profile={"name": "eddy-custom"}))
    assert session.send("質問です").ok
    assert recorded and all(
        req.get("profile") == {"name": "eddy-custom"} for req in recorded)


def test_subprocess_transport_without_profile_sends_no_key():
    # プロファイル無しのとき、要求の形は従来と1つも変えない。
    recorded = []
    runner = FakeRunner()

    def spy(request, timeout):
        recorded.append(dict(request))
        return runner(request, timeout)

    session = copilot_io.CopilotSession(copilot_io.SubprocessTransport(spy))
    assert session.send("質問です").ok
    assert recorded and all("profile" not in req for req in recorded)


def _fill_probe_runner(request, timeout):
    op = request["op"]
    if op == "snapshot":
        return {"ok": True, "title": "Copilot", "text_length": 10}
    if op == "input":
        return {"ok": True, "input_text": ""}
    if op == "wait_sendable":
        return {"ok": True, "done": True, "elapsed": 0.0}
    if op == "fill":
        return {"ok": True, "filled": True}
    if op == "probe":
        return {"ok": True, "candidates": [{"automation_id": "ai"}]}
    return {"ok": True}


def test_session_fill_and_probe_through_subprocess_transport():
    session = copilot_io.CopilotSession(
        copilot_io.SubprocessTransport(_fill_probe_runner))
    assert session.fill("テスト").ok
    probed = session.probe()
    assert probed.ok and probed.candidates == [{"automation_id": "ai"}]


def test_list_profiles_from_worker_reply():
    def runner(request, timeout):
        assert request["op"] == "profiles"
        return {"ok": True, "profiles": [
            {"name": "mscopilot"}, {"bad": 1}, {"name": "m365"}]}

    names = [p["name"] for p in copilot_io.list_profiles(runner=runner)]
    assert names == ["mscopilot", "m365"]


def test_list_profiles_worker_error_raises():
    def runner(request, timeout):
        return {"ok": False, "error": "壊れています"}

    try:
        copilot_io.list_profiles(runner=runner)
    except copilot_io.WorkerError:
        return
    raise AssertionError("WorkerError になるはず")


def test_worker_dispatch_fill_probe_profiles():
    import copilot_io_worker

    class FakeLoopTransport:
        last_profile = "unset"

        def __init__(self, profile=None):
            FakeLoopTransport.last_profile = profile

        def fill_prompt(self, text):
            return True

        def probe_input(self):
            return [{"automation_id": "in1"}]

        def close(self):
            pass

    real = copilot_io.CopilotLoopTransport
    copilot_io.CopilotLoopTransport = FakeLoopTransport
    try:
        filled = copilot_io_worker._run(
            {"op": "fill", "text": "abc", "profile": "m365"})
        # 添付の結果のキー (1.58) が常に付く。添付を持たない
        # transport では空になる。
        assert filled == {"ok": True, "filled": True,
                          "attach_done": [], "attach_failed": []}
        assert FakeLoopTransport.last_profile == "m365"
        probed = copilot_io_worker._run({"op": "probe"})
        assert probed == {"ok": True,
                           "candidates": [{"automation_id": "in1"}]}
        profiles = copilot_io_worker._run({"op": "profiles"})
        assert profiles["ok"]
        assert [p["name"] for p in profiles["profiles"]] == [
            "mscopilot", "m365"]
    finally:
        copilot_io.CopilotLoopTransport = real


def test_loop_transport_resolves_profile_name():
    import copilot_loop

    seen = {}

    class FakeCopilot:
        def __init__(self, profile=None, app_settings=None):
            seen["profile"] = profile

    real = copilot_loop.Copilot
    copilot_loop.Copilot = FakeCopilot
    try:
        transport = copilot_io.CopilotLoopTransport(profile="m365")
        transport._ensure()
        assert seen["profile"]["name"] == "m365"
        assert seen["profile"]["process_name"] == "M365Copilot.exe"
        bad = copilot_io.CopilotLoopTransport(profile="無い名前")
        try:
            bad._ensure()
        except RuntimeError:
            pass
        else:
            raise AssertionError("見つからない名前は RuntimeError のはず")
    finally:
        copilot_loop.Copilot = real


def test_latest_download_carries_candidates():
    """候補の全件 (1-57): session の latest_download は candidates
    を運び、持たない経路では None のまま。"""
    cand_a = {"name": "a", "class_name": "cls group/card"}
    cand_b = {"name": "b", "class_name": "cls group/card"}

    class ScanTransport:
        def latest_scan(self):
            return {"candidate": cand_b,
                    "candidates": [cand_a, cand_b]}

    session = copilot_io.CopilotSession(ScanTransport())
    result = session.latest_download()
    assert result.candidate == cand_b
    assert result.candidates == [cand_a, cand_b]

    class PlainTransport:
        def latest_scan(self):
            return {"candidate": cand_a}

    result2 = copilot_io.CopilotSession(PlainTransport()) \
        .latest_download()
    assert result2.candidate == cand_a
    assert result2.candidates is None


def test_press_download_passes_key_through_layers():
    """押下の key (1-57): worker の依頼に乗り、運び役の presser
    まで届くこと。key が無ければ依頼に含めないこと。"""
    import copilot_io_worker

    seen = {}

    class FakeLoopTransport:
        def __init__(self, profile=None):
            pass

        def press_download(self, key=None):
            seen["key"] = key
            return True

        def close(self):
            pass

    real = copilot_io.CopilotLoopTransport
    copilot_io.CopilotLoopTransport = FakeLoopTransport
    try:
        key = {"name": "n", "class_name": "cls group/card"}
        result = copilot_io_worker._run(
            {"op": "press_download", "key": key})
        assert result == {"ok": True, "pressed": True}
        assert seen["key"] == key
    finally:
        copilot_io.CopilotLoopTransport = real

    requests = []

    def runner(request, _timeout):
        requests.append(request)
        return {"ok": True, "pressed": True}

    transport = copilot_io.SubprocessTransport(runner=runner)
    assert transport.press_download(
        {"name": "n", "class_name": "c"}) is True
    assert requests[-1]["key"] == {"name": "n", "class_name": "c"}
    assert transport.press_download() is True
    assert "key" not in requests[-1]


def test_copilot_loop_transport_forwards_key():
    """CopilotLoopTransport は key を press_download_candidate へ
    そのまま渡す (1-57)。"""
    seen = {}

    class FakeCopilot:
        def press_download_candidate(self, key=None):
            seen["key"] = key
            return True

    transport = copilot_io.CopilotLoopTransport()
    transport._cp = FakeCopilot()  # _ensure が作らずに返す差し込み
    key = {"name": "n", "class_name": "cls group/card"}
    assert transport.press_download(key) is True
    assert seen["key"] == key


def test_latest_download_carries_diag_marker():
    """見張りの走査 (1-43): 候補と診断の目印が1回の結果に両方
    乗ること。scan を持たない transport では目印は None。"""
    class ScanTransport:
        def latest_scan(self):
            return {"candidate": {"name": "x"}, "marker": {"id": "abc"}}

        def close(self):
            pass

    session = copilot_io.CopilotSession(ScanTransport())
    result = session.latest_download()
    assert result.ok
    assert result.candidate == {"name": "x"}
    assert result.marker == {"id": "abc"}

    class PlainTransport:
        def latest_download(self):
            return None

        def close(self):
            pass

    session = copilot_io.CopilotSession(PlainTransport())
    result = session.latest_download()
    assert result.ok and result.candidate is None
    assert result.marker is None


def test_latest_download_carries_rerun_marker():
    """見張りの走査 (1.63): 再実行の目印 (command_id つき) が
    1回の結果に乗ること。形が違う目印は None に倒れること。"""
    class ScanTransport:
        def latest_scan(self):
            return {"candidate": None,
                    "rerun_marker": {"id": "x1", "key": "k1",
                                     "command_id": "1-99"}}

        def close(self):
            pass

    session = copilot_io.CopilotSession(ScanTransport())
    result = session.latest_download()
    assert result.ok
    assert result.rerun_marker == {"id": "x1", "key": "k1",
                                   "command_id": "1-99"}

    class BadTransport:
        def latest_scan(self):
            return {"candidate": None, "rerun_marker": "文字列"}

        def close(self):
            pass

    session = copilot_io.CopilotSession(BadTransport())
    result = session.latest_download()
    assert result.ok and result.rerun_marker is None


def test_latest_download_carries_tool_marker():
    """見張りの走査 (1.98、受け渡しの欠落を 1.99.1 で修正):
    ツール実行の目印 (command つき) が裏方→窓の全経路を
    通って 1回の結果に乗ること。形が違う目印は None に
    倒れること。"""
    class ScanTransport:
        def latest_scan(self):
            return {"candidate": None,
                    "tool_marker": {"id": "t1", "key": "k1",
                                    "command": "ipconfig"}}

        def close(self):
            pass

    session = copilot_io.CopilotSession(ScanTransport())
    result = session.latest_download()
    assert result.ok
    assert result.tool_marker == {"id": "t1", "key": "k1",
                                  "command": "ipconfig"}

    class BadTransport:
        def latest_scan(self):
            return {"candidate": None, "tool_marker": "文字列"}

        def close(self):
            pass

    session = copilot_io.CopilotSession(BadTransport())
    result = session.latest_download()
    assert result.ok and result.tool_marker is None


def test_worker_dispatch_latest_download_includes_marker():
    """裏方の latest_download (1-43、リセットは 1-52、候補の
    全件は 1-57、再実行は 1.63): scan があれば目印も候補の
    全件も返す。"""
    import copilot_io_worker

    class FakeLoopTransport:
        def __init__(self, profile=None):
            pass

        def latest_scan(self):
            return {"candidate": None, "marker": {"id": "w1"},
                    "reset_marker": {"id": "r1"},
                    "rerun_marker": {"id": "x1",
                                     "command_id": "1-99"},
                    "tool_marker": {"id": "t1",
                                    "command": "ipconfig"},
                    "relay_marker": None}

        def close(self):
            pass

    real = copilot_io.CopilotLoopTransport
    copilot_io.CopilotLoopTransport = FakeLoopTransport
    try:
        result = copilot_io_worker._run({"op": "latest_download"})
        assert result == {"ok": True, "candidate": None,
                          "candidates": None,
                          "marker": {"id": "w1"},
                          "reset_marker": {"id": "r1"},
                          "rerun_marker": {"id": "x1",
                                            "command_id": "1-99"},
                          "resend_marker": None,
                          "settings_marker": None,
                          "manual_marker": None,
                          "file_marker": None,
                          "fulltext_marker": None,
                          "capture_marker": None,
                          "history_marker": None,
                          "stop_marker": None,
                          "log_marker": None,
                          "tool_marker": {"id": "t1",
                                          "command": "ipconfig"},
                          "relay_marker": None,
                          "describe_marker": None,
                          "eval_marker": None,
                          "input_marker": None,
                          "overwrite_marker": None,
                          "force_marker": None}
    finally:
        copilot_io.CopilotLoopTransport = real


def test_subprocess_transport_latest_scan():
    """裏方への依頼 (1-43、リセットは 1-52、候補の全件は 1-57、
    再実行は 1.63): latest_download の応答から、候補と目印を
    取り出すこと。"""
    seen = {}

    def runner(request, _timeout):
        seen.update(request)
        return {"ok": True, "candidate": {"name": "y"},
                "marker": {"id": "m1"}, "reset_marker": {"id": "r1"},
                "rerun_marker": {"id": "x1", "command_id": "1-99"},
                "tool_marker": {"id": "t1", "command": "ipconfig"}}

    transport = copilot_io.SubprocessTransport(runner=runner)
    scan = transport.latest_scan()
    assert seen["op"] == "latest_download"
    assert scan == {"candidate": {"name": "y"}, "candidates": None,
                    "marker": {"id": "m1"},
                    "reset_marker": {"id": "r1"},
                    "rerun_marker": {"id": "x1",
                                     "command_id": "1-99"},
                    "resend_marker": None,
                    "settings_marker": None,
                    "manual_marker": None,
                    "file_marker": None,
                    "fulltext_marker": None,
                    "capture_marker": None,
                    "history_marker": None,
                    "stop_marker": None,
                    "log_marker": None,
                    "tool_marker": {"id": "t1",
                                    "command": "ipconfig"},
                    "relay_marker": None,
                    "describe_marker": None,
                    "eval_marker": None,
                    "input_marker": None,
                    "overwrite_marker": None,
                    "force_marker": None}
    assert transport.latest_download() == {"name": "y"}


# ---------------------------------------------------------------------------
# 添付 (1.58)
# ---------------------------------------------------------------------------
class AttachFakeCp:
    """Copilot の代わり (CopilotLoopTransport 用)。

    attach_files / set_input / send_prompt の呼ばれる順序と、
    入った本文を記録する。fail_names にあるファイル名は、
    attach_files が失敗として返す。"""

    def __init__(self, fail_names=()):
        self.calls = []
        self.fail_names = set(fail_names)
        self.draft = ""
        self.input_text_value = None
        self.sent_text = None

    def read_input(self):
        return self.draft

    def attach_files(self, paths):
        self.calls.append(("attach", list(paths)))
        return [os.path.basename(p) for p in paths
                if os.path.basename(p) in self.fail_names]

    def attach_test_file(self, path):
        self.calls.append(("attach_test", str(path)))
        return True, "添付できました（方式: 貼り付け）。チップも外しました"

    def set_input(self, text):
        self.calls.append(("fill", text))
        self.input_text_value = text

    def send_prompt(self, text):
        self.calls.append(("send", text))
        self.sent_text = text
        return True

    def close(self):
        pass


def test_loop_transport_attaches_before_fill():
    """添付は本文を入れる前に呼ばれる。失敗分は本文の末尾に
    1行足され、できた・できなかった名前が transport に残る。"""
    transport = copilot_io.CopilotLoopTransport()
    cp = AttachFakeCp(fail_names={"bad.txt"})
    transport._cp = cp
    ok = transport.fill_prompt(
        "結果です", attachments=["/out/ok.txt", "/out/bad.txt"])
    assert ok
    assert cp.calls[0] == ("attach", ["/out/ok.txt", "/out/bad.txt"])
    assert cp.calls[1][0] == "fill"
    assert cp.input_text_value == \
        "結果です\n添付できませんでした：bad.txt"
    assert transport.attach_done == ["ok.txt"]
    assert transport.attach_failed == ["bad.txt"]


def test_loop_transport_attaches_before_send():
    """送信の経路でも、添付が先。失敗が無ければ本文はそのまま。
    添付が無ければ attach_files は呼ばれない (従来どおり)。"""
    transport = copilot_io.CopilotLoopTransport()
    cp = AttachFakeCp()
    transport._cp = cp
    assert transport.send_prompt("本文", attachments=["/out/a.csv"])
    assert [c[0] for c in cp.calls] == ["attach", "send"]
    assert cp.sent_text == "本文"
    assert transport.attach_done == ["a.csv"]
    assert transport.attach_failed == []

    transport2 = copilot_io.CopilotLoopTransport()
    cp2 = AttachFakeCp()
    transport2._cp = cp2
    assert transport2.send_prompt("本文だけ")
    assert cp2.calls == [("send", "本文だけ")]


def test_session_fill_carries_attach_outcome():
    """セッションの結果に、添付できた・できなかった名前が残る。"""
    transport = copilot_io.CopilotLoopTransport()
    transport._cp = AttachFakeCp(fail_names={"bad.txt"})
    result = copilot_io.CopilotSession(transport).fill(
        "結果です", attachments=["/out/ok.txt", "/out/bad.txt"])
    assert result.ok
    assert result.attach_done == ["ok.txt"]
    assert result.attach_failed == ["bad.txt"]


def test_attach_test_via_loop_transport_and_session():
    """添付テスト (1.61): transport → セッション → 近道の順に、
    (成功か, 文) が SendResult で返る。送信はしない。"""
    transport = copilot_io.CopilotLoopTransport()
    cp = AttachFakeCp()
    transport._cp = cp
    ok, message = transport.attach_test("/out/a.txt")
    assert ok and "添付できました" in message
    assert cp.calls == [("attach_test", "/out/a.txt")]

    transport2 = copilot_io.CopilotLoopTransport()
    transport2._cp = AttachFakeCp()
    result = copilot_io.attach_test("/out/b.txt", transport=transport2)
    assert result.ok
    assert "チップも外しました" in result.message


def test_attach_test_via_subprocess_transport():
    """裏方経由の添付テスト: op と path が依頼に入り、応答の
    attach_ok / message が戻る。"""
    seen = {}

    def runner(request, timeout):
        seen.update(request)
        return {"ok": True, "attach_ok": False,
                "message": "添付できませんでした（段: not_found）: 出ず"}

    transport = copilot_io.SubprocessTransport(runner=runner)
    ok, message = transport.attach_test("/out/a.txt")
    assert not ok
    assert "not_found" in message
    assert seen["op"] == "attach_test"
    assert seen["path"] == "/out/a.txt"


def test_worker_dispatch_attach_test():
    """裏方の op "attach_test" が transport の結果を返す。"""
    import copilot_io_worker

    class FakeLoopTransport:
        def __init__(self, profile=None):
            pass

        def attach_test(self, path):
            return True, f"添付できました: {path}"

        def close(self):
            pass

    real = copilot_io.CopilotLoopTransport
    copilot_io.CopilotLoopTransport = FakeLoopTransport
    try:
        result = copilot_io_worker._run(
            {"op": "attach_test", "path": "/out/a.txt"})
        assert result == {"ok": True, "attach_ok": True,
                          "message": "添付できました: /out/a.txt"}
    finally:
        copilot_io.CopilotLoopTransport = real


def test_session_fill_with_draft_attaches_nothing():
    """書きかけがあって確認待ちのときは、添付もまだしない
    (fill 自体が走らないので、添付の段も踏まない)。"""
    transport = copilot_io.CopilotLoopTransport()
    cp = AttachFakeCp()
    cp.draft = "書きかけの文"
    transport._cp = cp
    result = copilot_io.CopilotSession(transport).fill(
        "結果です", attachments=["/out/a.txt"])
    assert not result.ok and result.needs_confirm
    assert cp.calls == []


def test_subprocess_transport_passes_attachments():
    """添付は裏方への依頼に乗り、結果がセッションの結果に残る。
    添付が無ければ、依頼に attachments を含めない (形は従来どおり)。"""
    recorded = []

    def runner(request, timeout):
        recorded.append(dict(request))
        op = request["op"]
        if op == "snapshot":
            return {"ok": True, "length": 10}
        if op == "input":
            return {"ok": True, "input_text": ""}
        if op == "wait_sendable":
            return {"ok": True, "done": True, "elapsed": 0.0}
        if op == "fill":
            if "attachments" in request:
                return {"ok": True, "filled": True,
                        "attach_done": ["a.txt"],
                        "attach_failed": ["b.txt"]}
            return {"ok": True, "filled": True}
        raise AssertionError(op)

    transport = copilot_io.SubprocessTransport(runner)
    result = copilot_io.CopilotSession(transport).fill(
        "結果です", attachments=["/out/a.txt", "/out/b.txt"])
    assert result.ok
    assert result.attach_done == ["a.txt"]
    assert result.attach_failed == ["b.txt"]
    fill_req = [r for r in recorded if r["op"] == "fill"][0]
    assert fill_req["attachments"] == ["/out/a.txt", "/out/b.txt"]

    recorded.clear()
    result2 = copilot_io.CopilotSession(transport).fill("結果です")
    assert result2.ok and result2.attach_done == []
    fill_req2 = [r for r in recorded if r["op"] == "fill"][0]
    assert "attachments" not in fill_req2


def test_worker_dispatch_fill_with_attachments():
    """裏方は attachments を transport へ渡し、添付の結果を
    応答に含める (1.58)。"""
    import copilot_io_worker

    class FakeLoopTransport:
        last = None

        def __init__(self, profile=None):
            self.attach_done = ["a.txt"]
            self.attach_failed = ["b.txt"]
            FakeLoopTransport.last = self

        def fill_prompt(self, text, attachments=None):
            self.got = (text, attachments)
            return True

        def close(self):
            pass

    real = copilot_io.CopilotLoopTransport
    copilot_io.CopilotLoopTransport = FakeLoopTransport
    try:
        filled = copilot_io_worker._run(
            {"op": "fill", "text": "abc",
             "attachments": ["/out/a.txt", "/out/b.txt"]})
        assert filled == {"ok": True, "filled": True,
                          "attach_done": ["a.txt"],
                          "attach_failed": ["b.txt"]}
        assert FakeLoopTransport.last.got == (
            "abc", ["/out/a.txt", "/out/b.txt"])
    finally:
        copilot_io.CopilotLoopTransport = real


def test_session_press_send_with_expected_through_subprocess():
    """方式2 の送信 (2.59): セッションが press_match を裏方へ流す。"""
    runner = FakeRunner()
    session = copilot_io.CopilotSession(
        copilot_io.SubprocessTransport(runner))
    result = session.press_send(expected="想定の文")
    assert result.ok
    assert runner.requests == ["wait_sendable", "press_match"]


def test_session_fill_lenient_through_subprocess():
    """方式2 の書き込み (2.59): lenient が裏方への依頼に乗る。"""
    recorded = []

    def runner(request, timeout):
        recorded.append(dict(request))
        op = request["op"]
        if op == "input":
            return {"ok": True, "input_text": ""}
        if op == "wait_sendable":
            return {"ok": True, "done": True, "elapsed": 0.0}
        if op == "fill":
            return {"ok": True, "filled": True}
        raise AssertionError(op)

    session = copilot_io.CopilotSession(
        copilot_io.SubprocessTransport(runner))
    assert session.fill("結果です", lenient=True).ok
    fill_req = [r for r in recorded if r["op"] == "fill"][0]
    assert fill_req.get("lenient") is True
    recorded.clear()
    assert session.fill("結果です").ok
    fill_req2 = [r for r in recorded if r["op"] == "fill"][0]
    assert "lenient" not in fill_req2


def test_worker_dispatch_press_match():
    """裏方は press_match を transport へ渡す (2.59)。"""
    import copilot_io_worker

    class FakeLoopTransport:
        last = None

        def __init__(self, profile=None):
            FakeLoopTransport.last = self

        def press_send_if_text(self, expected, timeout):
            self.got = (expected, timeout)
            return True

        def close(self):
            pass

    real = copilot_io.CopilotLoopTransport
    copilot_io.CopilotLoopTransport = FakeLoopTransport
    try:
        result = copilot_io_worker._run(
            {"op": "press_match", "text": "想定の文", "timeout": 7})
        assert result == {"ok": True, "pressed": True,
                          "forced_ratio": None}
        assert FakeLoopTransport.last.got == ("想定の文", 7.0)
    finally:
        copilot_io.CopilotLoopTransport = real


def test_session_press_send_forced_through_subprocess():
    """方式3 (2.61): 一致率つきで押せたら、結果の文に残る。"""
    runner = FakeRunner()
    session = copilot_io.CopilotSession(
        copilot_io.SubprocessTransport(runner))
    result = session.press_send(
        expected="想定の文", force_threshold=0.9)
    assert result.ok
    assert "大部分が一致したため送信しました" in result.message
    assert "95%" in result.message
    assert runner.requests == ["wait_sendable", "press_match"]


def test_worker_dispatch_press_match_forced():
    """裏方は force_threshold を渡し、一致率を返す (2.61)。"""
    import copilot_io_worker

    class FakeLoopTransport:
        last = None

        def __init__(self, profile=None):
            FakeLoopTransport.last = self

        def press_send_if_text(self, expected, timeout,
                               force_threshold=None):
            self.got = (expected, timeout, force_threshold)
            return True, 0.93

        def close(self):
            pass

    real = copilot_io.CopilotLoopTransport
    copilot_io.CopilotLoopTransport = FakeLoopTransport
    try:
        result = copilot_io_worker._run(
            {"op": "press_match", "text": "想定の文",
             "timeout": 7, "force_threshold": 0.9})
        assert result == {"ok": True, "pressed": True,
                          "forced_ratio": 0.93}
        assert FakeLoopTransport.last.got == ("想定の文", 7.0, 0.9)
    finally:
        copilot_io.CopilotLoopTransport = real


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
