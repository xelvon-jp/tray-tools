# agent_loop.py
# Copilot アプリを相手にした「疑似エージェントループ」の本体。
#
# 何をやるか
# ----------
# 1周 = プロンプト送信 → 応答受信 → #start/#end のスニペット抽出 → 危険検査 →
#       PowerShell 実行 → 実行結果を Copilot に貼り戻す
# を上限周回まで自動で回す。1周目のプロンプトは呼び出し側が渡す。
#
# 【安全の考え方】
# - **キー送信もマウス操作もしない。** 入力は UIA の ValuePattern、送信は
#   ボタンの InvokePattern。フォーカスを奪わないので、陽太さんが裏で作業していても
#   誤入力事故が起きない(tray-tools の CLAUDE.md の SetForegroundWindow 禁止と
#   同じ思想)。
# - **危険パターン(risky_lines)にヒットしたら実行せずに止める。** その旨を
#   Copilot に返して人の判断を待つ。自動で「別の書き方でお願いします」と繰り返す
#   ような挙動はしない(Copilot が押し切って危ないコードを別表現で出してくる罠がある)。
# - **タイムアウトは3層。** PowerShell 単発、応答待ち、ループ全体。どれかに引っ掛かれば
#   止まる。無限ループにならない。
# - **キャンセルはファイルで受ける。** copilot_loop フォルダ配下の cancel フラグを
#   置けば次の周の頭で止まる。IPC を経由しないので、ループ実行スレッドが忙しくても効く。
# - **既定は自動実行 OFF(dry_run=True)。** 初回は目視モードで、Copilot が出したコードを
#   ログに残すだけ。実運用に上げるときは明示的に auto を指定する。
#
# 【ログ】
# JSON Lines(1行1件)。各周の start/end、コード、実行結果、停止理由を全部残す。
# hooks/hook.log と同じ流儀で、個人の使用履歴なので .gitignore に入れる。
#
# 【依存】
# - copilot_loop.Copilot(UIA。PySide6 は不要)
# - subprocess(PowerShell を呼ぶ)
# - 標準ライブラリだけ。tray-tools 本体側と切り離して動く。
import argparse
import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# tray-tools 配下のモジュールを import できるようにする。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import copilot_loop  # noqa: E402  (ctypes+comtypes だけ、Qt を読まない)

# --- 既定値 -----------------------------------------------------------------
# ループの上限。10周もあれば大抵の題材は終わる。Copilot が延々と修正を続ける
# ループを止めるのが主目的なので、あまり大きくしない。
DEFAULT_MAX_ROUNDS = 10

# PowerShell 1回あたりの上限(秒)。長い集計でもこの範囲を想定。
# 超えたら kill して「タイムアウト」として Copilot に返す。
DEFAULT_PS_TIMEOUT = 60

# 1周ぶんの応答待ち上限(秒)。Copilot の応答は普通10〜30秒。長すぎたら異常。
DEFAULT_RESPONSE_TIMEOUT = 180

# 貼り戻す出力の最大文字数。長すぎる出力は Copilot の解釈も雑になるので切る。
# 頭と末尾の両方を残す(エラーは末尾に、成功サマリは先頭に出やすい)。
DEFAULT_PASTE_LIMIT = 3000

# ログの置き場所。個人の使用履歴なので .gitignore で追跡外にする。
LOG_PATH = _HERE / "copilot_loop.log"

# キャンセル用のファイル。次の周の頭で見て、あれば止める。
CANCEL_FLAG = _HERE / ".copilot_loop_cancel"

# 停止理由の型。ログにそのまま残す。
STOP_MAX_ROUNDS = "max-rounds"
STOP_NO_SNIPPET = "no-snippet"
STOP_RISKY = "risky-code"
STOP_TIMEOUT_RESPONSE = "response-timeout"
STOP_TIMEOUT_LOOP = "loop-timeout"
STOP_CANCEL = "cancelled"
STOP_DRY_RUN = "dry-run"
STOP_FINISH_WORD = "finish-word"
STOP_ERROR = "error"


# ---------------------------------------------------------------------------
# ログ
# ---------------------------------------------------------------------------
def _log(record: dict) -> None:
    """1行1件で JSONL を書く。落ちないこと(記録のためにループを止めない)。"""
    try:
        record.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
        with open(LOG_PATH, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# PowerShell 実行
# ---------------------------------------------------------------------------
def _write_snippet_file(code: str, snippet_id: str) -> Path:
    """スニペットを .ps1 として書き出す。UTF-8 BOM + CRLF が PowerShell 5.1 の作法。

    ヒアドキュメントで長いコードを PowerShell に渡すのは quoting の落とし穴が多い
    (シングル引用の中にシングル引用がある、絵文字が化ける等)ので、
    ファイルに書き出してから実行する方が確実。"""
    scratch = _HERE / "copilot_loop_scratch"
    scratch.mkdir(exist_ok=True)
    path = scratch / f"snippet_{snippet_id}_{int(time.time())}.ps1"
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(code)
    return path


def _run_powershell(code: str, snippet_id: str, timeout: int) -> dict:
    """PowerShell 5.1 で実行して、結果を辞書で返す。

    出力の UTF-8 化と cp932 のコンソールが混ざると化けるので、実行の頭で
    OutputEncoding を UTF-8 にする(コンソール表示自体は既定のままでよい。
    ここで欲しいのは stdout/stderr を UTF-8 で受け取ることだけ)。"""
    path = _write_snippet_file(code, snippet_id)
    wrapper = (
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"& '{path}'"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", wrapper],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "timed_out": False,
            "path": str(path),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "exit_code": None,
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
            "timed_out": True,
            "path": str(path),
        }


# ---------------------------------------------------------------------------
# 貼り戻し用の整形
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    """頭と末尾を残して真ん中を省略。エラーは末尾に、成功サマリは頭に出やすい。"""
    text = text or ""
    if len(text) <= limit:
        return text
    half = (limit - 20) // 2
    return text[:half] + "\n…（中略 %d 文字省略）…\n" % (len(text) - half * 2) + text[-half:]


def format_paste(snippet_id: str, result: dict, paste_limit: int) -> str:
    """実行結果を Copilot に貼り戻す文字列に整形する。

    テンプレは snippets/エージェントループ開始.txt の作法(エラーを貼ったら
    「原因の一言 + 修正後のスニペット全体」だけ返してもらう)に合わせる。"""
    if result.get("timed_out"):
        head = f"#{snippet_id} を実行しましたが、{DEFAULT_PS_TIMEOUT} 秒でタイムアウトしました。"
    elif result.get("exit_code") == 0:
        head = f"#{snippet_id} を実行しました。終了コード 0、エラーなしです。"
    else:
        head = f"#{snippet_id} を実行しました。終了コード {result.get('exit_code')}、エラーがあります。"

    parts = [head, ""]
    stdout = _clip(result.get("stdout") or "", paste_limit)
    if stdout.strip():
        parts += ["=== 標準出力 ===", stdout.rstrip(), ""]
    stderr = _clip(result.get("stderr") or "", paste_limit)
    if stderr.strip():
        parts += ["=== 標準エラー ===", stderr.rstrip(), ""]
    if not stdout.strip() and not stderr.strip():
        parts += ["（出力なし）", ""]
    parts += [
        "エラーがあれば「原因の一言 + 修正後のスニペット全体」だけ返してください。",
        "問題なければ次のステップへ進めてください。",
    ]
    return "\n".join(parts)


def format_risky_report(snippet_id: str, risks: list) -> str:
    """危険パターンを検出したときに Copilot へ返す文面。"""
    lines = [
        f"#{snippet_id} は危険パターンに触れるので実行しませんでした。",
        "自動実行を中断します。人の判断が必要です。",
        "",
        "=== 危険と判定した行 ===",
    ]
    for line, why in risks[:10]:
        lines.append(f"[{why}] {line}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 完了語
# ---------------------------------------------------------------------------
# 応答にこれが含まれたら「完了とみなす」（Ralph Wiggum 風）。既定は空(使わない)。
# プロンプトで「終わったら <DONE> と書いてください」のように仕込んでおく前提。
FINISH_WORD_RE_TEMPLATE = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])"


def _matches_finish_word(text: str, finish_word: str) -> bool:
    if not finish_word:
        return False
    return re.search(FINISH_WORD_RE_TEMPLATE.format(re.escape(finish_word)), text or "") is not None


# ---------------------------------------------------------------------------
# ループ本体
# ---------------------------------------------------------------------------
def run_loop(
    initial_prompt=None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    ps_timeout: int = DEFAULT_PS_TIMEOUT,
    response_timeout: int = DEFAULT_RESPONSE_TIMEOUT,
    paste_limit: int = DEFAULT_PASTE_LIMIT,
    finish_word: str = "",
    auto_run: bool = False,
    loop_timeout: int = 30 * 60,
    watch: bool = False,
    on_event=None,
) -> dict:
    """疑似エージェントループを1回まわす。結果のサマリを辞書で返す。

    【モード】
    - initial_prompt を渡すと従来モード: 1周目に tray-tools が送信する。
    - watch=True にすると監視モード: 1周目の送信をスキップし、いきなり
      応答受信から始める。**人が Copilot に直接お題を打った後**に開始する用。
      業務PCで Claude Code が無い環境向け。

    【実行の切り替え】
    - auto_run=False(既定) は dry-run。Copilot が返したコードを実行せずログに
      残して停止する。新しい題材はまずここで安全に確かめる。
    - auto_run=True で初めて PowerShell に流す。危険パターン検出でそのまま止まる。

    【イベント配信】
    - on_event を渡すと、進捗イベント(response / snippet / run / stop など)が
      その呼び出し可能に流れる。Qt のログ窓に反映するために使う。呼び出しは
      ワーカースレッド。受け側で Qt をキュー接続などで受け直すこと。
    """
    started = time.time()
    _cancel_clear()

    def emit(kind, **extra):
        payload = {"event": kind, **extra}
        _log(payload)
        if on_event is not None:
            try:
                on_event(payload)
            except Exception as e:  # noqa: BLE001  受け側で失敗してもループを止めない
                print(f"[agent_loop] on_event 失敗: {e}", file=sys.stderr)

    emit("loop_start",
         prompt_chars=len(initial_prompt or ""),
         max_rounds=max_rounds, auto_run=auto_run, watch=watch,
         ps_timeout=ps_timeout, response_timeout=response_timeout,
         loop_timeout=loop_timeout, finish_word=finish_word)

    cp = copilot_loop.Copilot()
    try:
        initial_state = cp.state()
        if initial_state == "busy" and not watch:
            emit("loop_end", reason=STOP_ERROR,
                 detail="起動時点で Copilot が回答中")
            return {"stopped_by": STOP_ERROR, "rounds": 0,
                    "detail": "Copilot が回答中でした。終わってから始めてください。"}

        prompt = initial_prompt or ""
        rounds = 0
        stopped_by = STOP_MAX_ROUNDS
        stop_detail = ""

        while rounds < max_rounds:
            if _cancel_requested():
                stopped_by, stop_detail = STOP_CANCEL, "cancel フラグを検知"
                break
            if time.time() - started > loop_timeout:
                stopped_by, stop_detail = STOP_TIMEOUT_LOOP, f"ループ全体で {loop_timeout} 秒を超過"
                break

            rounds += 1
            round_started = time.time()
            # 監視モードの1周目は送信をスキップ(人が Copilot に既に送っている想定)。
            # 2周目以降は普通の送信になる。
            skip_send = watch and rounds == 1
            emit("round_start", round=rounds, skip_send=skip_send,
                 prompt_preview=(prompt or "")[:120])

            if skip_send:
                # 監視モードの1周目。「人が Copilot に投稿したお題への応答」を取りたいが、
                # snapshot_length を使うと、開始ボタンを押すまでに Copilot が既に応答を
                # 書き終わっていた場合に「全文長より後ろ = 空」となってしまう
                # (実測: 5.9秒で 0 文字が返り、no-snippet で終わった)。
                #
                # 代わりに「最後の user_marker(あなたの発言)」の位置を previous_length に
                # する。応答が完了していても書きかけでも、正しく「人の最後の発言以降」を
                # 拾える。user_marker が無い(会話履歴がまっさら)なら 0 から取る。
                text = cp.document_text()
                # マーカーが空のプロファイル(M365 Copilot は 'あなたの発言' 相当の
                # Text を出していない)では位置を決めようがないので全文を対象にする。
                # rfind("") は len(text) を返すため、空を弾かないと常に空応答になる。
                user_marker = cp.profile.get("user_marker") or ""
                idx = text.rfind(user_marker) if user_marker else -1
                previous_length = 0 if idx == -1 else idx + len(user_marker)
            else:
                # 1) 送信直前の全文長を控える(new_response が使う)
                previous_length = cp.snapshot_length()
                try:
                    cp.set_input(prompt)
                except Exception as e:  # noqa: BLE001  UIA は多様に落ちうる
                    stopped_by, stop_detail = STOP_ERROR, f"入力欄に書けませんでした: {e}"
                    break
                try:
                    sent = cp.click_send()
                except Exception as e:  # noqa: BLE001
                    stopped_by, stop_detail = STOP_ERROR, f"送信ボタンを押せませんでした: {e}"
                    break
                if not sent:
                    stopped_by, stop_detail = STOP_ERROR, "送信ボタンが見つかりません"
                    break

            # 2) 完了待ち
            done, wait_elapsed = cp.wait_until_idle(timeout=response_timeout)
            if not done:
                stopped_by = STOP_TIMEOUT_RESPONSE
                stop_detail = f"応答待ちで {response_timeout} 秒を超えました"
                emit("round_end", round=rounds,
                     reason=stopped_by, elapsed=time.time() - round_started)
                break

            # 3) 新規応答を取得
            # 送った本文を渡す。発言マーカーを持たないアプリ(M365 Copilot)では、
            # これが「どこまでが自分の発言か」を知る唯一の手掛かりになる。
            # 監視モードの1周目は人が手で投稿しているので、こちらは本文を知らない。
            response = cp.new_response(previous_length,
                                       sent_prompt=None if skip_send else prompt)
            emit("response", round=rounds, chars=len(response),
                 wait_seconds=round(wait_elapsed, 1),
                 response_head=response[:800])

            # 4) 完了語チェック(コードより先に見る。コード内の変数名にヒットしても
            #    「完了語で止まる」方が事故が少ない)
            if _matches_finish_word(response, finish_word):
                stopped_by = STOP_FINISH_WORD
                stop_detail = f"応答に完了語 {finish_word!r} が現れました"
                emit("round_end", round=rounds,
                     reason=stopped_by, elapsed=time.time() - round_started)
                break

            # 5) スニペット抽出
            snippets = copilot_loop.extract_snippets(response)
            if not snippets:
                stopped_by = STOP_NO_SNIPPET
                stop_detail = "応答に #start/#end のスニペットがありません"
                emit("round_end", round=rounds,
                     reason=stopped_by, elapsed=time.time() - round_started,
                     response_tail=response[-500:])
                break

            # 最後の1つだけを扱う(複数出されたら仕様確認のため止める方が安全)
            sid, code = snippets[-1]
            risks = copilot_loop.risky_lines(code)
            emit("snippet", round=rounds, id=sid,
                 chars=len(code), risks=len(risks), code=code)

            if risks:
                stopped_by = STOP_RISKY
                stop_detail = f"#{sid} に危険パターン {len(risks)} 件"
                # Copilot に理由だけ伝える(応答は取らずに終わる。人が判断する場面)
                try:
                    cp.set_input(format_risky_report(sid, risks))
                except Exception:  # noqa: BLE001  ここは best-effort
                    pass
                emit("round_end", round=rounds,
                     reason=stopped_by, elapsed=time.time() - round_started,
                     risky_lines=[{"line": ln, "reason": rr} for ln, rr in risks])
                break

            # 6) 実行(auto_run のときだけ)
            if not auto_run:
                stopped_by = STOP_DRY_RUN
                stop_detail = f"dry-run。#{sid}({len(code)}文字) は実行せず、ログに残しました"
                emit("dry_run", round=rounds, id=sid, code=code)
                break

            result = _run_powershell(code, sid, ps_timeout)
            emit("run", round=rounds, id=sid,
                 exit_code=result.get("exit_code"),
                 timed_out=result.get("timed_out"),
                 stdout_chars=len(result.get("stdout") or ""),
                 stderr_chars=len(result.get("stderr") or ""),
                 stdout=result.get("stdout") or "",
                 stderr=result.get("stderr") or "")

            # 7) 次のプロンプトを組み立てて次周へ
            prompt = format_paste(sid, result, paste_limit)
            emit("round_end", round=rounds,
                 elapsed=time.time() - round_started)

        total = time.time() - started
        emit("loop_end", reason=stopped_by, detail=stop_detail,
             rounds=rounds, elapsed=round(total, 1))
        return {
            "stopped_by": stopped_by, "detail": stop_detail,
            "rounds": rounds, "elapsed": round(total, 1),
        }
    finally:
        # 掴んだのと同じスレッドで COM を手放す。GC 任せにすると解放が
        # 別スレッドまで先送りされ、アパートメントを跨いで 0xC0000005 で落ちる。
        # run_loop はワーカースレッドで回るので、ここが要になる。
        try:
            cp.close()
        except Exception:  # noqa: BLE001  finally の例外はどこにも捕まらない
            pass


# ---------------------------------------------------------------------------
# キャンセル
# ---------------------------------------------------------------------------
def _cancel_requested() -> bool:
    return CANCEL_FLAG.exists()


def _cancel_clear() -> None:
    try:
        CANCEL_FLAG.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def request_cancel() -> None:
    """外部からキャンセルを要求する。次の周の頭で拾って止まる。"""
    try:
        CANCEL_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_prompt(path: str) -> str:
    return io.open(path, encoding="utf-8-sig").read().strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Copilot アプリで疑似エージェントループを回す。")
    parser.add_argument("prompt_file", nargs="?", default=None,
                        help="1周目のプロンプトを書いたテキストファイル(--watch では不要)")
    parser.add_argument("--watch", action="store_true",
                        help="監視モード: 人が Copilot に投稿した直後から引き取って回す")
    parser.add_argument("--auto", action="store_true",
                        help="実行係も自動化(危険パターン検出時は止まる)")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--ps-timeout", type=int, default=DEFAULT_PS_TIMEOUT)
    parser.add_argument("--response-timeout", type=int, default=DEFAULT_RESPONSE_TIMEOUT)
    parser.add_argument("--paste-limit", type=int, default=DEFAULT_PASTE_LIMIT)
    parser.add_argument("--finish-word", default="",
                        help="応答に含まれたら完了とみなす語。例: DONE")
    parser.add_argument("--loop-timeout", type=int, default=30 * 60)
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    if args.watch:
        prompt = None
    else:
        if not args.prompt_file:
            print("prompt_file を指定するか --watch を付けてください", file=sys.stderr)
            return 2
        prompt = _load_prompt(args.prompt_file)
    summary = run_loop(
        initial_prompt=prompt, watch=args.watch,
        max_rounds=args.max_rounds, ps_timeout=args.ps_timeout,
        response_timeout=args.response_timeout, paste_limit=args.paste_limit,
        finish_word=args.finish_word, auto_run=args.auto,
        loop_timeout=args.loop_timeout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["stopped_by"] in (STOP_FINISH_WORD, STOP_DRY_RUN,
                                          STOP_NO_SNIPPET) else 1


if __name__ == "__main__":
    sys.exit(main())
