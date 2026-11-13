# copilot_io.py
# Copilot へ「送る・待つ・取る」の共通入口(仕様: collab-workspace の
# docs/work-tools/spec.md「共通部品」)。案A のステップ実行パネルと
# 案B のテキストエディタで共用する。
#
# 作りの線引き:
#   - UIA を直接叩くのは copilot_loop だけ。このモジュールは、その上に
#     薄く「入口を3つにそろえる」ことだけをする
#   - 失敗は例外で落とさず、結果オブジェクト(ok と利用者向け message)で返す。
#     Copilot が起動していない・送信ボタンが見つからない・応答が時間切れ、
#     はどれも「利用者に伝える文」になる
#   - transport を差し替えられる。単体テストは fake の transport で、
#     UIA の無い環境でも送る/待つ/取るの流れを確かめる
#
# 使い方:
#   session = CopilotSession()
#   result = session.send("質問や、実行結果の貼り戻し")
#   if result.ok:
#       session.wait()
#       response = session.last_response()   # .text と .code_blocks
#   session.close()
#
# 送信だけなら send_text(text) でもよい(セッションを作って送って閉じる)。
import time
from dataclasses import dataclass, field

DEFAULT_WAIT_TIMEOUT = 300.0   # 応答待ちの既定の上限(秒)。agent_loop と同じ桁


@dataclass
class SendResult:
    ok: bool
    message: str


@dataclass
class WaitResult:
    ok: bool
    message: str
    elapsed: float = 0.0


@dataclass
class ResponseResult:
    ok: bool
    message: str
    text: str = ""
    code_blocks: list = field(default_factory=list)


class CopilotLoopTransport:
    """copilot_loop.Copilot の薄いラッパー。

    Copilot オブジェクトは初めて使うときに作る(窓が無ければ RuntimeError)。
    作る・使う・閉じる(close)を同じスレッドで通すこと。COM オブジェクトを
    スレッド間で渡すと落ちる(tray-tools で何度も踏んだ罠。agent_loop が
    ワーカースレッドの中で全部を完結させているのと同じ作法)。"""

    def __init__(self):
        self._cp = None

    def _ensure(self):
        if self._cp is None:
            import copilot_loop  # 重く Windows 寄りなので使うときだけ読む

            self._cp = copilot_loop.Copilot()
        return self._cp

    def snapshot_length(self):
        return self._ensure().snapshot_length()

    def send_prompt(self, text):
        return self._ensure().send_prompt(text)

    def wait_until_idle(self, timeout):
        return self._ensure().wait_until_idle(timeout=timeout)

    def new_response(self, previous_length, sent_prompt=None):
        return self._ensure().new_response(previous_length, sent_prompt=sent_prompt)

    def code_blocks(self):
        return self._ensure().code_blocks()

    def close(self):
        if self._cp is not None:
            try:
                self._cp.close()
            finally:
                self._cp = None


class CopilotSession:
    """1回の往復(送る→待つ→取る)を束ねる。

    送信の直前に会話全文の長さを控え、取る側はその後ろだけを見る
    (copilot_loop.new_response の作法。全文からマーカーで切る旧方式は
    会話が長くなると誤爆するので使わない)。"""

    def __init__(self, transport=None):
        # transport はテスト用の差し替え口。通常は None で、初回利用時に
        # CopilotLoopTransport を作る(生成自体は軽い。窓探しはその先)。
        self._transport = transport
        self._previous_length = None
        self._sent_text = None

    def _tp(self):
        if self._transport is None:
            self._transport = CopilotLoopTransport()
        return self._transport

    # -- 送る -------------------------------------------------------------
    def send(self, text) -> SendResult:
        if not text or not text.strip():
            return SendResult(False, "送るテキストが空です")
        try:
            # 送信直前の全文長を控える。窓が無い環境ではここで落ちるので、
            # 「起動していない」を先に伝える形になる。
            self._previous_length = self._tp().snapshot_length()
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            return SendResult(False, f"Copilot に送れませんでした（{e}）")
        try:
            sent = self._tp().send_prompt(text)
        except Exception as e:  # noqa: BLE001
            return SendResult(False, f"Copilot に送れませんでした（{e}）")
        if not sent:
            return SendResult(
                False, "Copilot に送信できませんでした（送信ボタンが見つからない等）")
        self._sent_text = text
        return SendResult(True, "Copilot に送信しました")

    # -- 待つ -------------------------------------------------------------
    def wait(self, timeout=DEFAULT_WAIT_TIMEOUT) -> WaitResult:
        started = time.time()
        try:
            done, elapsed = self._tp().wait_until_idle(timeout)
        except Exception as e:  # noqa: BLE001
            return WaitResult(False, f"応答を待てませんでした（{e}）",
                              time.time() - started)
        if not done:
            return WaitResult(
                False, f"応答が {timeout:.0f} 秒以内に終わりませんでした", elapsed)
        return WaitResult(True, "応答が完了しました", elapsed)

    # -- 取る -------------------------------------------------------------
    def last_response(self) -> ResponseResult:
        try:
            previous = self._previous_length if self._previous_length is not None else 0
            text = self._tp().new_response(previous, sent_prompt=self._sent_text)
        except Exception as e:  # noqa: BLE001
            return ResponseResult(False, f"応答を取り出せませんでした（{e}）")
        blocks = []
        try:
            blocks = self._tp().code_blocks() or []
        except Exception:  # noqa: BLE001
            # コードブロックの目印を持たないプロファイル(M365 等)では取れない。
            # 本文が取れているなら、そちらだけでも返す。
            blocks = []
        return ResponseResult(True, "応答を取り出しました", text or "", list(blocks))

    def close(self):
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass


def send_text(text, transport=None) -> SendResult:
    """送信だけの近道。セッションを作って送って閉じる。

    エディタの「選択範囲を Copilot に投げる」のような、応答をこちらで
    読まない送り方のための入口。"""
    session = CopilotSession(transport)
    try:
        return session.send(text)
    finally:
        session.close()
