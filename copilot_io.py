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


@dataclass
class ProbeResult:
    """入力欄の検出(プローブ)の結果。candidates は
    [{"automation_id", "control_type", "name"}] の並び。"""
    ok: bool
    message: str
    candidates: list = field(default_factory=list)


@dataclass
class ScanResult:
    """入力欄の深い調査の結果。candidates は
    [{"control_type", "name", "automation_id", "class_name",
    "value_pattern", "text_pattern"}] の並び (copilot_loop の
    scan_input_candidates が返す形そのまま)。送信ボタンの調査
    (scan_buttons) も同じ形で、候補には "invoke_pattern" が付く。"""
    ok: bool
    message: str
    candidates: list = field(default_factory=list)


@dataclass
class DownloadResult:
    """チャットのダウンロード検出の結果。candidate は
    {"name", "class_name", "label", "summary"} (copilot_loop の
    latest_download_candidate が返す形) か、無ければ None。
    候補が無いのは通常の状態で ok=True。検出できない経路・窓が
    無い場合は ok=False で、理由が message に入る。
    marker は、同じ走査で見つけた診断リクエストの目印
    {"id": 同定子} (1-43) か、無ければ None。"""
    ok: bool
    message: str
    candidate: dict = None
    marker: dict = None


@dataclass
class ReadResult:
    """入力欄の読み直しの結果。message は、読めなかった段の理由
    (copilot_loop の段つきの文) をそのまま持つ。text は読めた文
    (読めなければ空)。"""
    ok: bool
    message: str
    text: str = ""


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

    def __init__(self, runner=None, op_timeout=WORKER_OP_TIMEOUT,
                 profile=None):
        self._runner = runner if runner is not None else run_worker
        self._op_timeout = op_timeout
        # 投げ先のプロファイル(辞書か、組み込みの名前)。None なら
        # 依頼に含めない (裏方は従来どおり自動選択になる)。
        self._profile = profile

    def _call(self, request, timeout=None):
        if self._profile is not None:
            request = dict(request)
            request["profile"] = self._profile
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

    def fill_prompt(self, text):
        return bool(self._call({"op": "fill", "text": text})["filled"])

    def probe_input(self):
        return list(self._call({"op": "probe"})["candidates"])

    def scan_input(self):
        return list(self._call({"op": "scan"})["candidates"])

    def scan_buttons(self):
        return list(self._call({"op": "scan_buttons"})["candidates"])

    def press_send(self):
        return bool(self._call({"op": "press_send"})["pressed"])

    def latest_download(self):
        return self.latest_scan().get("candidate")

    def latest_scan(self):
        response = self._call({"op": "latest_download"})
        return {"candidate": response.get("candidate"),
                "marker": response.get("marker")}

    def press_download(self):
        return bool(self._call({"op": "press_download"})["pressed"])

    def send_escape(self):
        return str(self._call({"op": "send_escape"}).get("status")
                   or "failed")

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

    def __init__(self, profile=None):
        self._cp = None
        # 投げ先のプロファイル。辞書そのものか、組み込みの名前
        # (文字列)。None なら Copilot 側の自動選択に従来どおり任せる。
        self._profile = profile

    def _ensure(self):
        if self._cp is None:
            import copilot_loop  # 重く Windows 寄りなので使うときだけ読む

            profile = self._profile
            if isinstance(profile, str):
                # 名前は copilot_loop.profiles() の並びから引く
                # (settings の copilot_profiles が先勝ち、の作法も
                # あちらに任せる。ここでは app_settings を渡さない
                # ので、実質は組み込みの名前解決になる)。
                for candidate in copilot_loop.profiles():
                    if candidate.get("name") == profile:
                        profile = candidate
                        break
                else:
                    raise RuntimeError(
                        f"投げ先のプロファイルが見つかりません: {profile}")
            self._cp = copilot_loop.Copilot(profile=profile)
        return self._cp

    def snapshot_length(self):
        return self._ensure().snapshot_length()

    def input_text(self):
        """入力欄にいま入っている文。

        読めない(入力欄や ValuePattern が見つからない)ときは None を
        返さず RuntimeError を上げる。「空だった」と誤認したまま
        送信すると、書きかけの文を黙って上書きしかねないためで、
        CopilotSession.send は例外を失敗の結果に畳む(送らない)。

        copilot_loop 側が段つきの理由(InputStageError: 見つからない /
        中身が読めない)で上げた場合は、その文がそのまま呼び側へ伝わる。
        ここで一文に畳むのは、read_input が None を返す実装(テストの
        fake 等)のときだけ。"""
        value = self._ensure().read_input()
        if value is None:
            raise RuntimeError("Copilot の入力欄が読めませんでした")
        return value

    def send_prompt(self, text):
        return self._ensure().send_prompt(text)

    def fill_prompt(self, text):
        """入力欄に入れるだけ (送信ボタンは押さない)。"""
        self._ensure().set_input(text)
        return True

    def probe_input(self):
        return self._ensure().probe_input_candidates()

    def scan_input(self):
        return self._ensure().scan_input_candidates()

    def scan_buttons(self):
        return self._ensure().scan_button_candidates()

    def press_send(self):
        """プロファイルの目印 (send_*) の送信ボタンを Invoke で押す。

        失敗は copilot_loop の段つきの例外 (InputStageError) がそのまま
        上がる。CopilotSession.press_send が理由の文に畳む。"""
        return self._ensure().press_send_button()

    def latest_download(self):
        return self._ensure().latest_download_candidate()

    def latest_scan(self):
        return self._ensure().latest_chat_scan()

    def press_download(self):
        return self._ensure().press_download_candidate()

    def send_escape(self):
        return self._ensure().send_escape()

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

    def __init__(self, transport=None, profile=None):
        # transport はテスト用の差し替え口。通常は None で、初回利用時に
        # SubprocessTransport を作る(UIA は裏方プロセスの中だけ)。
        # profile は投げ先の指定 (辞書か組み込みの名前)。transport を
        # 自分で作るときだけ効く (差し替えられた transport は、その
        # 中で解決済みとして扱う)。
        self._transport = transport
        self._profile = profile
        self._previous_length = None
        self._sent_text = None

    def _tp(self):
        if self._transport is None:
            self._transport = SubprocessTransport(profile=self._profile)
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

    # -- 入れるだけ -------------------------------------------------------
    def fill(self, text, force=False) -> SendResult:
        """入力欄に text を入れるだけ。送信はしない。

        書きかけの扱いは send と同じ: force=False で入力欄に文が
        あれば、上書きせず needs_confirm=True で返す (テスト入力でも
        書きかけを消さない)。結果の形も send とそろえてある。"""
        if not text or not text.strip():
            return SendResult(False, "入れるテキストが空です")
        if not force:
            existing = None
            getter = getattr(self._tp(), "input_text", None)
            if getter is not None:
                try:
                    existing = getter()
                except Exception as e:  # noqa: BLE001
                    return SendResult(
                        False, f"入力欄に入れられませんでした（{e}）")
            if existing and existing.strip():
                return SendResult(
                    False,
                    "入力欄に書きかけの文があります。"
                    "テストでも上書きはしません",
                    needs_confirm=True)
        try:
            filler = getattr(self._tp(), "fill_prompt", None)
            if filler is None:
                return SendResult(
                    False, "この経路では入力欄に入れられません")
            filled = filler(text)
        except Exception as e:  # noqa: BLE001
            return SendResult(
                False, f"入力欄に入れられませんでした（{e}）")
        if not filled:
            return SendResult(False, "入力欄に入れられませんでした")
        return SendResult(True, "入力欄に入れました（送信はしていません）")

    # -- 検出 -------------------------------------------------------------
    def probe(self) -> ProbeResult:
        """入力欄の候補を検出する (プロファイル作りの下調べ)。"""
        try:
            prober = getattr(self._tp(), "probe_input", None)
            if prober is None:
                return ProbeResult(
                    False, "この経路では入力欄を検出できません")
            candidates = prober()
        except Exception as e:  # noqa: BLE001
            return ProbeResult(
                False, f"入力欄を検出できませんでした（{e}）")
        candidates = [c for c in (candidates or []) if isinstance(c, dict)]
        if not candidates:
            return ProbeResult(
                False, "入力欄の候補が見つかりませんでした")
        return ProbeResult(
            True, f"入力欄の候補が {len(candidates)} 件見つかりました",
            candidates)

    # -- 調査 -------------------------------------------------------------
    def scan(self) -> ScanResult:
        """入力欄の候補を深く調査する (PHAL の調査画面用)。

        probe より広く拾い、合わせるための目印を全部付けて返す
        (copilot_loop.scan_input_candidates)。窓が見つからなければ
        裏方側の理由がそのまま失敗の文になる。"""
        try:
            scanner = getattr(self._tp(), "scan_input", None)
            if scanner is None:
                return ScanResult(
                    False, "この経路では入力欄を調査できません")
            candidates = scanner()
        except Exception as e:  # noqa: BLE001
            return ScanResult(
                False, f"入力欄を調査できませんでした（{e}）")
        candidates = [c for c in (candidates or []) if isinstance(c, dict)]
        if not candidates:
            return ScanResult(
                False,
                "編集できそうな要素が見つかりませんでした。"
                "窓が開いているか、ページが読み込み終わっているか"
                "確かめてください")
        return ScanResult(
            True, f"候補が {len(candidates)} 件見つかりました",
            candidates)

    # -- 読み直す ---------------------------------------------------------
    def read_input(self) -> ReadResult:
        """入力欄の今の文を読み直す (PHAL の送信前の確認用)。

        読めなければ、段つきの理由 (見つからない / 中身が読めない) が
        そのまま message になる。「空だった」と誤認したまま送信ボタン
        を押さないよう、失敗と空は結果の形でも分けてある。"""
        try:
            getter = getattr(self._tp(), "input_text", None)
            if getter is None:
                return ReadResult(False, "この経路では入力欄を読めません")
            value = getter()
        except Exception as e:  # noqa: BLE001  段つきの文をそのまま伝える
            return ReadResult(False, str(e) or "入力欄が読めませんでした")
        if value is None:
            return ReadResult(False, "入力欄が読めませんでした")
        return ReadResult(True, "入力欄を読みました", str(value))

    # -- 送信ボタンを押す ---------------------------------------------------
    def press_send(self) -> SendResult:
        """プロファイルの目印 (send_*) の送信ボタンを押す。

        入力欄への投入 (fill) とは別の操作で、こちらは本当に送信する。
        失敗は段つきの理由 (窓が無い / ボタンが見つからない・候補つき /
        押す段で失敗) がそのまま message になる。"""
        try:
            presser = getattr(self._tp(), "press_send", None)
            if presser is None:
                return SendResult(
                    False, "この経路では送信ボタンを押せません")
            pressed = presser()
        except Exception as e:  # noqa: BLE001  段つきの文をそのまま伝える
            return SendResult(False, str(e) or "送信ボタンを押せませんでした")
        if not pressed:
            return SendResult(False, "送信ボタンを押せませんでした")
        return SendResult(True, "送信ボタンを押しました")

    # -- 送信ボタンの調査 ---------------------------------------------------
    def scan_buttons(self) -> ScanResult:
        """送信ボタンの候補を調査する (PHAL の調査画面用)。

        入力欄の scan と同じ形で、種類が Button のものに「押せるか
        (InvokePattern)」を付けて返す。"""
        try:
            scanner = getattr(self._tp(), "scan_buttons", None)
            if scanner is None:
                return ScanResult(
                    False, "この経路では送信ボタンを調査できません")
            candidates = scanner()
        except Exception as e:  # noqa: BLE001
            return ScanResult(
                False, f"送信ボタンを調査できませんでした（{e}）")
        candidates = [c for c in (candidates or []) if isinstance(c, dict)]
        if not candidates:
            return ScanResult(
                False,
                "ボタンが見つかりませんでした。"
                "窓が開いているか、ページが読み込み終わっているか"
                "確かめてください")
        return ScanResult(
            True, f"候補が {len(candidates)} 件見つかりました",
            candidates)

    # -- チャットの DL 検出・押下 ------------------------------------------
    def latest_download(self) -> DownloadResult:
        """最新の AI 発言にあるダウンロード候補を検出する
        (PHAL の見張り用)。

        候補が無ければ ok=True で candidate=None (窓はあるが落とす
        ものが無い、という通常の状態)。窓が見つからない等の失敗は
        ok=False で理由が message になる。呼ぶ側 (見張り) は
        どちらでも黙って扱える。同じ走査で見つけた診断リクエスト
        の目印 (1-43) は marker に入れて返す (走査は1回だけ)。"""
        try:
            scanner = getattr(self._tp(), "latest_scan", None)
            if scanner is not None:
                scan = scanner() or {}
                candidate = scan.get("candidate")
                marker = scan.get("marker")
            else:
                getter = getattr(self._tp(), "latest_download", None)
                if getter is None:
                    return DownloadResult(
                        False, "この経路ではダウンロードを検出できません")
                candidate = getter()
                marker = None
        except Exception as e:  # noqa: BLE001
            return DownloadResult(
                False, str(e) or "ダウンロードを検出できませんでした")
        if candidate is not None and not isinstance(candidate, dict):
            candidate = None
        if marker is not None and not isinstance(marker, dict):
            marker = None
        if candidate is None:
            return DownloadResult(
                True, "ダウンロードできるものはありません", None, marker)
        return DownloadResult(
            True, "ダウンロードできるものがあります", candidate, marker)

    def press_download(self) -> SendResult:
        """最新のダウンロード候補を押す (本当に落とす)。

        失敗は段つきの理由 (窓が無い / 候補が消えた / 押す段で
        失敗) がそのまま message になる。"""
        try:
            presser = getattr(self._tp(), "press_download", None)
            if presser is None:
                return SendResult(
                    False, "この経路ではダウンロードボタンを押せません")
            pressed = presser()
        except Exception as e:  # noqa: BLE001  段つきの文をそのまま伝える
            return SendResult(
                False, str(e) or "ダウンロードボタンを押せませんでした")
        if not pressed:
            return SendResult(False, "ダウンロードボタンを押せませんでした")
        return SendResult(True, "ダウンロードボタンを押しました")

    def send_escape(self) -> SendResult:
        """対象の窓へ ESC を送る (1-67)。

        DL の到着後に Edge のダウンロード・フライアウトを閉じる
        ための standalone 操作。状態語 (sent / no-hwnd / failed)
        は文にそのまま残す (PHAL の診断の控えに残るため)。
        投げられたかどうかまでで、本当に閉じたかは分からない。"""
        try:
            sender = getattr(self._tp(), "send_escape", None)
            if sender is None:
                return SendResult(
                    False, "ESC送信: この経路では送れません")
            status = sender()
        except Exception as e:  # noqa: BLE001  理由は文にして返す
            return SendResult(False, f"ESC送信: failed（{e}）")
        if status == "sent":
            return SendResult(True, "ESC送信: sent")
        return SendResult(False, f"ESC送信: {status or 'failed'}")

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


def send_text(text, transport=None, force=False, profile=None) -> SendResult:
    """送信だけの近道。セッションを作って送って閉じる。

    エディタの「選択範囲を AI に投げる」のような、応答をこちらで
    読まない送り方のための入口。force の意味は CopilotSession.send と同じ。
    profile は投げ先の指定 (辞書か組み込みの名前)。None なら従来どおり
    自動選択。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.send(text, force=force)
    finally:
        session.close()


def fill_text(text, transport=None, force=False, profile=None) -> SendResult:
    """入力欄に入れるだけの近道 (送信はしない)。テスト入力用。

    force の意味は CopilotSession.fill と同じ (書きかけがあっても
    上書きする)。profile の意味は send_text と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.fill(text, force=force)
    finally:
        session.close()


def probe_input(profile=None, transport=None) -> ProbeResult:
    """入力欄の候補を検出する近道。profile の意味は send_text と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.probe()
    finally:
        session.close()


def scan_input(profile=None, transport=None) -> ScanResult:
    """入力欄の候補を深く調査する近道。profile の意味は send_text
    と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.scan()
    finally:
        session.close()


def read_input_text(profile=None, transport=None) -> ReadResult:
    """入力欄の今の文を読み直す近道。profile の意味は send_text と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.read_input()
    finally:
        session.close()


def press_send_button(profile=None, transport=None) -> SendResult:
    """プロファイルの目印 (send_*) の送信ボタンを押す近道。

    本当に送信する操作なので、呼ぶのは人の押下に応じるときだけ
    (PHAL の「送信」ボタン)。profile の意味は send_text と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.press_send()
    finally:
        session.close()


def scan_send_buttons(profile=None, transport=None) -> ScanResult:
    """送信ボタンの候補を調査する近道。profile の意味は send_text
    と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.scan_buttons()
    finally:
        session.close()


def latest_download(profile=None, transport=None) -> DownloadResult:
    """チャットのダウンロード候補を検出する近道。profile の意味は
    send_text と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.latest_download()
    finally:
        session.close()


def press_download_button(profile=None, transport=None) -> SendResult:
    """最新のダウンロード候補を押す近道 (本当に落とす)。

    呼ぶのは人の押下 (PHAL の「DL」ボタン) か、自動化の切替が
    入っているときだけ。profile の意味は send_text と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.press_download()
    finally:
        session.close()


def send_escape(profile=None, transport=None) -> SendResult:
    """対象の窓へ ESC を送る近道 (1-67)。

    DL の到着が確かめられた直後に、PHAL の窓側から呼ばれる
    (Edge のダウンロード・フライアウトを閉じるため)。
    profile の意味は send_text と同じ。"""
    session = CopilotSession(transport, profile=profile)
    try:
        return session.send_escape()
    finally:
        session.close()


def list_profiles(runner=None, timeout=WORKER_OP_TIMEOUT) -> list:
    """使えるプロファイルの並びを裏方から取る (組み込み + 裏方の
    解決順)。設定画面が「組み込み」の一覧を出すために使う。

    取れなければ WorkerError (呼び側が「自動とカスタムだけ」に
    倒せるように、結果オブジェクトではなく例外で返す)。"""
    run = runner if runner is not None else run_worker
    response = run({"op": "profiles"}, timeout)
    if not isinstance(response, dict) or not response.get("ok"):
        detail = (response.get("error") if isinstance(response, dict)
                  else None) or "裏方から読めない応答が返りました"
        raise WorkerError(str(detail))
    return [p for p in (response.get("profiles") or [])
            if isinstance(p, dict) and p.get("name")]
