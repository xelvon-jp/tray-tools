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
                 blocks=("print(1)",), snapshot=100, fail_at=None):
        self._send_ok = send_ok
        self._idle = idle
        self._response = response
        self._blocks = list(blocks)
        self._snapshot = snapshot
        self._fail_at = fail_at
        self.sent = []
        self.new_response_args = None
        self.closed = False

    def _maybe_fail(self, where):
        if self._fail_at == where:
            raise RuntimeError("Copilot の窓が見つかりません")

    def snapshot_length(self):
        self._maybe_fail("snapshot")
        return self._snapshot

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
