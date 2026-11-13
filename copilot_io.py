# copilot_io.py
# Copilot へ「送る・待つ・取る」の共通入口(仕様: collab-workspace の
# docs/work-tools/spec.md「共通部品」)。案A のステップ実行パネルと
# 案B のテキストエディタで共用する。
#
# 作りの線引き:
#   - UIA を直接叩くのは copilot_loop だけ。しかも、それを動かすのは
#     **裏方プロセス(copilot_io_worker.py)の中だけ**。常駐の中で UIA を
#     動かすと、pycaw と同じプロセスに comtypes が同居して、GC の拍子に
#     常駐ごと即死する(実測あり。copilot_status_process.py の冒頭)。
#     だから CopilotSession の操作は、入口がトレイでも あっふ でも、
#     常にサブプロセス経由の1本の経路にそろえる
#   - 失敗は例外で落とさず、結果オブジェクト(ok と利用者向け message)で返す。
#     Copilot が起動していない・送信ボタンが見つからない・応答が時間切れ、
#     裏方の時間切れ・異常終了、はどれも「利用者に伝える文」になる
#   - transport を差し替えられる。単体テストは fake の transport や
#     fake の裏方実行役で、UIA の無い環境でも流れを確かめる
#
# 使い方:
#   session = CopilotSession()
#   result = session.send("質問や、実行結果の貼り戻し")
#   if result.needs_confirm:
#       # 入力欄に書きかけの文がある。利用者に確かめてから
#       result = session.send("...", force=True)
#   if result.ok:
#       session.wait()
#       response = session.last_response()   # .text と .code_blocks
#   session.close()
#
# 送信だけなら send_text(text) でもよい(セッションを作って送って閉じる)。
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

DEFAULT_WAIT_TIMEOUT = 300.0   # 応答待ちの既定の上限(秒)。agent_loop と同じ桁
WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "copilot_io_worker.py")
# 裏方1回の上限(秒)。UIA の走査が重いときでも、この桁で足りる。
# 応答待ち(wait)だけは、待ち時間そのものに余裕を足して別に決める。
WORKER_OP_TIMEOUT = 90.0
WORKER_WAIT_MARGIN = 60.0


@dataclass
class SendResult:
    ok: bool
    message: str
    # 入力欄に書きかけの文があり、まだ送っていない(利用者の確認待ち)。
    # True のときは、確かめてから force=True で送り直す。
    needs_confirm: bool = False


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


class WorkerError(RuntimeError):
    """裏方プロセスとのやり取りの失敗。文はそのまま利用者に見せられる
    日本語にしておく(CopilotSession が結果オブジェクトへ畳む)。"""


# ---------------------------------------------------------------------------
# 裏方プロセスの実行役
# ---------------------------------------------------------------------------
def run_worker(request, timeout, argv=None):
    """copilot_io_worker.py を1回起こして、依頼(JSON化できる辞書)を渡し、
    応答の辞書を返す。

    応答は標準出力の最後の JSON 行から取る(裏方が余計な行を出しても、
    JSON でない行は読み飛ばす。agent_loop の読み取りと同じ作法)。
    時間切れ・起こせない・応答が読めない、は WorkerError にする。"""
    if argv is None:
        argv = [sys.executable, WORKER_PATH]
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise WorkerError(
            f"Copilot の裏方が {timeout:.0f} 秒以内に終わりませんでした") from None
    except OSError as e:
        raise WorkerError(f"Copilot の裏方を起こせませんでした（{e}）") from None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    tail = (proc.stderr or "").strip().splitlines()
    detail = tail[-1] if tail else f"終了コード {proc.returncode}"
    raise WorkerError(f"Copilot の裏方が異常終了しました（{detail}）")


class SubprocessTransport:
    """copilot_io_worker.py 経由で copilot_loop を使う transport。

    操作ごとに裏方を1回起こす(状態は Copilot アプリ側にあるので、
    送る→待つ→取るを別々の裏方に分けてもつながる)。runner は
    テスト用の差し替え口で、(依頼の辞書, 上限秒) → 応答の辞書。"""

    def __init__(self, runner=None, op_timeout=WORKER_OP_TIMEOUT):
        self._runner = runner if runner is not None else run_worker
        self._op_timeout = op_timeout

    def _call(self, request, timeout=None):
        response = self._runner(
            request, self._op_timeout if timeout is None else timeout)
        if not isinstance(response, dict):
            raise WorkerError("Copilot の裏方から読めない応答が返りました")
        if not response.get("ok"):
            raise WorkerError(
                str(response.get("error") or "Copilot の裏方で失敗しました"))
        return response

    def snapshot_length(self):
        return int(self._call({"op": "snapshot"})["length"])

    def input_text(self):
        return self._call({"op": "input"})["input_text"]

    def send_prompt(self, text):
        return bool(self._call({"op": "send", "text": text})["sent"])

    def wait_until_idle(self, timeout):
        response = self._call({"op": "wait", "timeout": timeout},
                              timeout=timeout + WORKER_WAIT_MARGIN)
        return bool(response["done"]), float(response["elapsed"])

    def new_response(self, previous_length, sent_prompt=None):
        response = self._call({
            "op": "response",
            "previous_length": previous_length,
            "sent_prompt": sent_prompt,
        })
        return response["text"]

    def code_blocks(self):
        return list(self._call({"op": "blocks"})["blocks"])

    def close(self):
        pass  # 裏方は操作ごとに終わるので、閉じるものは無い


class CopilotLoopTransport:
    """copilot_loop.Copilot の薄いラッパー。

    **これは裏方プロセス(copilot_io_worker.py)の中で使うためのもの。**
    常駐や あっふ のプロセスから直接使うと、UIA(comtypes)がその
    プロセスに同居してしまう(冒頭の注記)。テストで流れだけ見る場合も、
    実 UIA を叩かない fake を使うこと。

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

    def input_text(self):
        """入力欄にいま入っている文。

        読めない(入力欄や ValuePattern が見つからない)ときは None を
        返さず RuntimeError を上げる。「空だった」と誤認したまま
        送信すると、書きかけの文を黙って上書きしかねないためで、
        CopilotSession.send は例外を失敗の結果に畳む(送らない)。"""
        value = self._ensure().read_input()
        if value is None:
            raise RuntimeError("Copilot の入力欄が読めませんでした")
        return value

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
        # SubprocessTransport を作る(UIA は裏方プロセスの中だけ)。
        self._transport = transport
        self._previous_length = None
        self._sent_text = None

    def _tp(self):
        if self._transport is None:
            self._transport = SubprocessTransport()
        return self._transport

    # -- 送る -------------------------------------------------------------
    def send(self, text, force=False) -> SendResult:
        """送る。force=False のとき、入力欄に書きかけの文があれば
        送らずに needs_confirm=True で返す(send_prompt は入力欄を
        上書きして即送信するので、書きかけが黙って消えるのを防ぐ)。"""
        if not text or not text.strip():
            return SendResult(False, "送るテキストが空です")
        try:
            # 送信直前の全文長を控える。窓が無い環境ではここで落ちるので、
            # 「起動していない」を先に伝える形になる。
            self._previous_length = self._tp().snapshot_length()
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            return SendResult(False, f"Copilot に送れませんでした（{e}）")
        if not force:
            existing = None
            getter = getattr(self._tp(), "input_text", None)
            if getter is not None:
                try:
                    existing = getter()
                except Exception as e:  # noqa: BLE001
                    return SendResult(
                        False, f"Copilot に送れませんでした（{e}）")
            if existing and existing.strip():
                return SendResult(
                    False,
                    "Copilot の入力欄に書きかけの文があります。"
                    "そのまま送ると上書きされて消えます",
                    needs_confirm=True)
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


def send_text(text, transport=None, force=False) -> SendResult:
    """送信だけの近道。セッションを作って送って閉じる。

    エディタの「選択範囲を Copilot に投げる」のような、応答をこちらで
    読まない送り方のための入口。force の意味は CopilotSession.send と同じ。"""
    session = CopilotSession(transport)
    try:
        return session.send(text, force=force)
    finally:
        session.close()
