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
        if op == "wait":
            return {"ok": True, "done": True, "elapsed": 2.0}
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
    assert runner.requests == ["snapshot", "input", "send",
                               "wait", "response", "blocks"]


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
