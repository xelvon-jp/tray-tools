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
# - **危険パターン(risky_lines)にヒットしたら実行せずに止める。** 人が見て
#   「承知のうえで実行してよい」と答えたときだけ、その周に限って実行する
#   (承認は覚えない。次に出てきたらまた聞く)。答えが無ければ止まる。
#   自動で「別の書き方でお願いします」と繰り返すような挙動はしない
#   (Copilot が押し切って危ないコードを別表現で出してくる罠がある)。
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
import threading
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

# 実行が何周続けて失敗したら諦めるか。実測で、壊れたコードを渡してしまったときに
# Copilot が誤診して同じ失敗を8周繰り返した。3周も同じなら人が見たほうが早い。
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

# 監視モードで「新しい応答」を待つ上限(秒)。押してから Copilot に投稿する使い方を
# 想定して、少し長めに取る。ここを過ぎたら何もせず終わる(勝手に走り出さない)。
DEFAULT_WATCH_TIMEOUT = 180

# 貼り戻す出力の最大文字数。長すぎる出力は Copilot の解釈も雑になるので切る。
# 頭と末尾の両方を残す(エラーは末尾に、成功サマリは先頭に出やすい)。
DEFAULT_PASTE_LIMIT = 3000

# ログの置き場所。個人の使用履歴なので .gitignore で追跡外にする。
LOG_PATH = _HERE / "copilot_loop.log"

# キャンセル用のファイル。次の周の頭で見て、あれば止める。
CANCEL_FLAG = _HERE / ".copilot_loop_cancel"

# 親(常駐)からの返事を受け取るファイル。止めるか続けるかを人に聞く場面で使う。
#
# 【なぜファイルか】
# 進捗は子の標準出力で親へ流しているが、逆向きの経路が無かった。標準入力を使う手も
# あるが、キャンセルが既にファイルで往復できているので同じ流儀に揃える。
# ファイルなら、子が別のことで塞がっていても書ける。
DECISION_FILE = _HERE / ".copilot_loop_decision"

# 人に聞いたあと、何秒待つか。過ぎたら「答えなし」＝止める(今までと同じ挙動)。
# 席を外している間に危険なコードが勝手に走らない、が既定であるべき。
DEFAULT_APPROVAL_TIMEOUT = 300

# スニペットの .ps1 を置く場所と、実行時の作業ディレクトリ。
#
# 【作業ディレクトリを分ける理由】
# 以前は tray-tools 直下で実行していた。Copilot が `.\out.csv` のような相対パスを
# 書くと、リポジトリ直下にファイルが生まれる。実際、身に覚えの無いファイルが repo に
# 残っていたことがある(public リポジトリなので事故になり得る)。散らかる先を一箇所に
# 決めておけば、掃除も .gitignore も1行で済む。
SCRATCH_DIR = _HERE / "copilot_loop_scratch"
WORK_DIR = SCRATCH_DIR / "work"

# 残しておく .ps1 の本数。振り返りに使うのはせいぜい直近の数周なので、それ以上は
# 溜めない(実測で38本まで溜まっていた)。work/ の中身は消さない — 成果物かもしれず、
# 自動で捨ててよいものではない。
KEEP_SNIPPET_FILES = 20

# 停止理由の型。ログにそのまま残す。
STOP_MAX_ROUNDS = "max-rounds"
STOP_NO_SNIPPET = "no-snippet"
STOP_RISKY = "risky-code"
STOP_TIMEOUT_RESPONSE = "response-timeout"
STOP_TIMEOUT_LOOP = "loop-timeout"
STOP_CANCEL = "cancelled"
STOP_DRY_RUN = "dry-run"
STOP_FINISH_WORD = "finish-word"
STOP_STUCK = "stuck"
STOP_NO_NEW_RESPONSE = "no-new-response"
STOP_MULTI_SNIPPET = "multi-snippet"
STOP_EMPTY_RESPONSE = "empty-response"
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
    SCRATCH_DIR.mkdir(exist_ok=True)
    # 名前は「時刻が先、ID が後」。ナノ秒まで入れる。
    #
    # 【秒だと足りない】
    # 以前は snippet_<ID>_<秒>.ps1 だった。同じ ID が同じ秒に2回出ると
    # **同じ名前になって上書き**され、前の周のコードが消えていた。さらに掃除の
    # 並べ替えも、秒までしか分からないと同着だらけになり、新しいほうを捨てることが
    # ある(実測: 25本書いて残った20本のうち9本が最新のものではなかった)。
    # 時刻を先頭に置いておけば、名前順がそのまま新しい順になる。
    path = SCRATCH_DIR / f"snippet_{time.time_ns()}_{snippet_id}.ps1"
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(code)
    _prune_snippet_files()
    return path


def _prune_snippet_files(keep: int = KEEP_SNIPPET_FILES) -> None:
    """古い .ps1 を捨てる。直近 keep 本だけ残す。

    振り返りに使うのは直近の数周ぶんだけなので、それ以上は溜めても読まない。
    消すのは自分が書いた snippet_*.ps1 だけ。work/ の中身(Copilot が作った成果物
    かもしれないもの)には触らない。掃除のために作業を止めたくないので、
    失敗しても黙って諦める。

    並べ替えは更新時刻ではなく名前で行う。名前の先頭にナノ秒の時刻が入っている
    ので名前順＝新しい順になる。更新時刻は環境によって秒までしか取れず、まとめて
    書いたファイルが同着になって、新しいほうを捨てることがあった。"""
    try:
        files = sorted(SCRATCH_DIR.glob("snippet_*.ps1"),
                       key=lambda p: p.name, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


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
    # 実行の作業ディレクトリは work/ に固定する。相対パスで書かれたファイルが
    # リポジトリ直下に散らばるのを防ぐため(WORK_DIR のコメント参照)。
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", wrapper],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(WORK_DIR),
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
def failed(result: dict) -> bool:
    """実行が失敗したか。**終了コードだけでは判定できない。**

    PowerShell は中で例外が出ても終了コード0を返すことがある。実測で
    「'-fixed' は認識されません」「パラメーターが見つかりません」が出ているのに
    0 だった。終了コードしか見ていなかったせいで、失敗が1回も数えられず、
    10周まわりきるまで止まらなかった。標準エラーに何か出ていたら失敗とみなす。"""
    if result.get("timed_out"):
        return True
    if result.get("exit_code") not in (0, None):
        return True
    return bool((result.get("stderr") or "").strip())


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
    elif not failed(result):
        head = f"#{snippet_id} を実行しました。終了コード 0、エラーなしです。"
    else:
        # **終了コードだけを見て「エラーなし」と言わないこと。**
        # PowerShell は中で例外が出ても終了コード0を返すことがある。実測で
        # 「-fixed は認識されません」「パラメーターが見つかりません」が出ているのに
        # 0 だった。それを「エラーなしです」と伝えたうえでエラー本文を貼っていたので、
        # Copilot は矛盾した材料を渡されて堂々巡りに入った。
        head = (f"#{snippet_id} を実行しました。終了コード {result.get('exit_code')} "
                "ですが、標準エラーに出力があります。失敗として扱ってください。"
                if result.get("exit_code") == 0 else
                f"#{snippet_id} を実行しました。終了コード {result.get('exit_code')}、"
                "エラーがあります。")

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


def _wait_for_new_response(cp, timeout, emit, poll=1.0, settle=2.0):
    """監視モードの入口で、新しい応答が来るまで待つ。来たら True。

    「新しい」の判定は、開始時点の全文長より伸びて、かつしばらく伸び止まったこと。
    生成中に開始を押した場合(busy)は、そのまま書き終わるのを待つ。

    ここで待たずに最後の応答を拾うと、前のやり取りが画面に残っているときに
    押した瞬間から走り出す。実測でそれが起きて、意図せず10周回った。
    「押してから投稿する」使い方もあるので、待つほうを既定にする。"""
    start_len = cp.snapshot_length()
    deadline = time.time() + max(0.0, timeout)
    emit("watch_waiting", chars=start_len, timeout=timeout)
    grew_at = None
    while time.time() < deadline:
        time.sleep(poll)
        try:
            now = cp.snapshot_length()
            busy = cp.state() == "busy"
        except Exception:  # noqa: BLE001  一時的に読めないだけなら次の周期で
            continue
        if busy:
            # 生成中。伸び止まりの判定はやり直す。
            grew_at = None
            continue
        if now > start_len:
            if grew_at is None:
                grew_at = time.time()
            elif time.time() - grew_at >= settle:
                return True
        else:
            grew_at = None
    return False


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
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    wait_for_new: bool = True,
    watch_timeout: int = DEFAULT_WATCH_TIMEOUT,
    approval_timeout: int = 0,
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

    【人に聞いて続ける】
    - approval_timeout > 0 なら、止まる前に一度だけ人に聞く(危険パターン・dry-run・
      スニペット複数の3場面)。答えが 'run' なら、その周だけ実行して続行する。
      答えが無いまま時間切れになれば、今までどおり止まる。0 なら聞かずに止まる。

    【イベント配信】
    - on_event を渡すと、進捗イベント(response / snippet / run / stop など)が
      その呼び出し可能に流れる。Qt のログ窓に反映するために使う。呼び出しは
      ワーカースレッド。受け側で Qt をキュー接続などで受け直すこと。
    """
    started = time.time()
    _cancel_clear()
    # 前回の走行が残した返事を持ち越さない(合言葉で弾けるが、紛らわしいので消す)。
    _clear_decision()

    # 第1引数の名前をアンダースコア始まりにしてあるのは、イベントの中身として
    # kind= や name= を渡したいことがあるため。普通の名前だと衝突して
    # 「got multiple values for argument」で落ちる。
    def emit(_event, **extra):
        payload = {"event": _event, **extra}
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

        consecutive_failures = 0
        # 直前の周の標準出力。同じ結果が続く＝進んでいない、の判定に使う。
        last_output = None
        repeated_outputs = 0
        # 直前の周に実行したコード。出力より先に「進んでいない」が分かる。
        last_code = None
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
                # 監視モードの1周目。「人が Copilot に投稿したお題への応答」を取る。
                #
                # 【まず新しい応答を待つ】
                # 以前はここで無条件に「最後の応答」を拾っていた。そのため、前回の
                # やり取りが画面に残っている状態で開始を押すと、**押した瞬間に古い
                # 応答を引き取って走り出した**(実測: 意図せず10周回った)。
                # 人が投稿するより先に押すこともあるので、まず新しい応答の到着を待つ。
                # 既に生成が終わっていれば待たずに進む。
                if wait_for_new:
                    fresh = _wait_for_new_response(cp, watch_timeout, emit)
                    if not fresh:
                        stopped_by = STOP_NO_NEW_RESPONSE
                        stop_detail = (
                            f"{watch_timeout} 秒待ちましたが、新しい応答が来ませんでした。"
                            "Copilot にお題を投稿してから始めてください。")
                        emit("round_end", round=rounds, reason=stopped_by,
                             elapsed=time.time() - round_started)
                        break

                # snapshot_length を使うと、Copilot が既に応答を書き終わっていた場合に
                # 「全文長より後ろ = 空」となってしまう(実測: 5.9秒で 0 文字)。
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
                # 書いて送る。空振りしたら一度だけ書き直して試す
                # (理由は copilot_loop.send_prompt を参照)。
                try:
                    sent = cp.send_prompt(prompt)
                except Exception as e:  # noqa: BLE001  UIA は多様に落ちうる
                    stopped_by, stop_detail = STOP_ERROR, f"送信できませんでした: {e}"
                    break
                if not sent:
                    # 見えていたボタン名を残す。「見つかりません」だけだと、次に
                    # 起きたときにまた推測から始めることになる(実際そうなった)。
                    seen = getattr(cp, "last_bottom_buttons", None) or []
                    stopped_by = STOP_ERROR
                    stop_detail = ("送信ボタンが見つかりません（書き直して2回試しました。"
                                   "そのとき見えていたボタン: "
                                   + ("・".join(seen) if seen else "読めず") + "）")
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

            # **空の応答を「終わった」と読まない。**
            #
            # wait_until_idle は busy を一度も見なくても終われる作りになっている
            # (busy を見逃して永久に待つ事故を避けるため)。その裏返しで、Copilot が
            # 書き始める前に「落ち着いた」と判断することがある。実測(2026-09-11)では
            # 送信から5.1秒で0文字を拾い、そのあと Copilot はちゃんと1207文字返して
            # いた。会話が長くなる(実測26,626文字)ほど最初の一文字までが遅くなる。
            #
            # 空のまま進むとスニペットが無いので no-snippet になるが、あれは
            # 「やり切った」ときの停止理由でもある。**競争に負けただけなのに完了と
            # 報告される**のがいちばん困るので、空のときは待ち直す。
            if not response.strip():
                deadline = time.time() + response_timeout
                while not response.strip() and time.time() < deadline:
                    emit("empty_response", round=rounds,
                         waited=round(wait_elapsed, 1))
                    done, more = cp.wait_until_idle(
                        timeout=max(5, min(30, int(deadline - time.time()))))
                    wait_elapsed += more
                    response = cp.new_response(
                        previous_length,
                        sent_prompt=None if skip_send else prompt)
                if not response.strip():
                    stopped_by = STOP_EMPTY_RESPONSE
                    stop_detail = (f"{response_timeout} 秒待っても応答が空のままでした。"
                                   "Copilot が返していないか、応答を読み取れていません。")
                    emit("round_end", round=rounds, reason=stopped_by,
                         elapsed=time.time() - round_started)
                    break

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

            # 扱うのは最後の1つ。ただし複数出されたときは黙って選ばない。
            #
            # 【なぜ止めるか】
            # Copilot は「まず調べる #1、それから直す #2」のように2つ出すことがある。
            # 最後だけを実行すると調査を飛ばして修正が走る。どちらを実行してほしいのかは
            # こちらには判断できないので、人に見せる。
            # (以前はコメントに「止める方が安全」と書きながら、黙って最後を実行していた。)
            sid, code = snippets[-1]
            if len(snippets) > 1:
                ids = "・".join(f"#{s}" for s, _c in snippets)
                emit("snippet", round=rounds, id=sid, chars=len(code),
                     risks=0, code=code, siblings=len(snippets))
                if _ask_approval(
                        emit, "multi-snippet",
                        f"スニペットが {len(snippets)} 個あります（{ids}）",
                        f"最後の #{sid} だけを実行して続けますか？\n\n{code}",
                        approval_timeout) != ANSWER_RUN:
                    stopped_by = STOP_MULTI_SNIPPET
                    stop_detail = (f"応答にスニペットが {len(snippets)} 個({ids})。"
                                   "どれを実行すべきか判断できないので止めました。")
                    emit("round_end", round=rounds,
                         reason=stopped_by, elapsed=time.time() - round_started)
                    break

            risks = copilot_loop.risky_lines(code)
            emit("snippet", round=rounds, id=sid,
                 chars=len(code), risks=len(risks), code=code)

            if risks:
                # 危険パターン。自動で押し切らせない場面なので、人に聞く。
                # 許可は**この周だけ**。次に出てきたらまた聞く(覚えさせない)。
                why = "・".join(sorted({rr for _ln, rr in risks}))
                answer = _ask_approval(
                    emit, "risky-code",
                    f"#{sid} が危険パターンに触れています（{why}）",
                    format_risky_report(sid, risks), approval_timeout)
                if answer != ANSWER_RUN:
                    stopped_by = STOP_RISKY
                    stop_detail = f"#{sid} に危険パターン {len(risks)} 件"
                    # 誰も答えなかったときだけ、Copilot の入力欄に理由を残す。
                    #
                    # 【自分で止めた人に「人の判断が必要です」と書かない】
                    # 承認を出すようにする前は、ここが唯一の伝え方だった。いまは
                    # 人が「ここで止める」を選んでいる場合があり、その人は理由を
                    # 分かっている。それでも書き込むと、判断が済んでいるのに
                    # 判断を求める文面が残り、しかも次に打つときに消す手間になる
                    # (実測で117文字)。答えが無かったときは、あとで気づく手がかりが
                    # 要るので今までどおり残す。
                    if answer == ANSWER_NONE:
                        try:
                            cp.set_input(format_risky_report(sid, risks))
                        except Exception:  # noqa: BLE001  ここは best-effort
                            pass
                    emit("round_end", round=rounds,
                         reason=stopped_by, elapsed=time.time() - round_started,
                         risky_lines=[{"line": ln, "reason": rr} for ln, rr in risks])
                    break
                emit("approval_override", round=rounds, id=sid,
                     kind="risky-code", risks=len(risks))

            # 6) 実行(auto_run のときだけ)
            if not auto_run:
                # dry-run。コードは取れていて、あとは実行するだけの状態。ここで完全に
                # 終わると、目で見て納得しても最初からやり直しになる。「これを実行して
                # 続ける」と答えられれば、安全確認の意味は保ったまま二度手間だけが消える。
                emit("dry_run", round=rounds, id=sid, code=code)
                if _ask_approval(
                        emit, "dry-run",
                        f"dry-run です。#{sid}（{len(code)}文字）を実行しますか？",
                        code, approval_timeout) != ANSWER_RUN:
                    stopped_by = STOP_DRY_RUN
                    stop_detail = (f"dry-run。#{sid}({len(code)}文字) は実行せず、"
                                   "ログに残しました")
                    break
                # この周だけ実行に回る。次の周はまた dry-run として聞く。
                emit("approval_override", round=rounds, id=sid, kind="dry-run")

            # 前の周とまったく同じコードが返ってきたら、そこで足踏みしている。
            #
            # 【出力の一致だけでは足りない】
            # 既に「同じ標準出力が続いたら中断」を入れてあるが、出力に時刻やパスが
            # 一つでも混ざると毎回違う文字列になり、検出をすり抜ける。コード自体が
            # 同じなら、Copilot は直したつもりで何も変えていないということで、
            # 実行するまでもなく結果は分かっている。1回で止めてよい。
            normalized = "\n".join(
                line.rstrip() for line in code.strip().splitlines())
            if last_code is not None and normalized == last_code:
                stopped_by = STOP_STUCK
                stop_detail = ("前の周とまったく同じコードが返ってきました。"
                               "直したつもりで変わっていないので中断します。")
                emit("round_end", round=rounds,
                     reason=stopped_by, elapsed=time.time() - round_started)
                break
            last_code = normalized

            result = _run_powershell(code, sid, ps_timeout)
            emit("run", round=rounds, id=sid,
                 exit_code=result.get("exit_code"),
                 # 成否は終了コードではなくこちらで判断する(failed の説明を参照)。
                 # 見る側が自分で終了コードから判断すると、**PowerShell が例外を
                 # 出しても0を返す**のでずれる。実測で、AssertionError で落ちた
                 # テストが exit=0 として緑で表示されていた。判断は1か所に置く。
                 failed=failed(result),
                 timed_out=result.get("timed_out"),
                 stdout_chars=len(result.get("stdout") or ""),
                 stderr_chars=len(result.get("stderr") or ""),
                 stdout=result.get("stdout") or "",
                 stderr=result.get("stderr") or "")

            # 失敗が続いたら諦める。
            #
            # 【なぜ要るか】
            # 実測で、10周のうち8周が同じエラーの堂々巡りになった。こちらが渡した
            # コードは画面から読んだ時点で壊れていたのに、Copilot はそれを自分の
            # 書き間違いだと誤診し、「直したつもりの同じコード」を出し続けた。
            # 上限まで回ればいずれ止まるが、その間ずっと実行を繰り返してしまう。
            # 進んでいないと分かった時点で人に返すほうがよい。
            if failed(result):
                consecutive_failures += 1
                stuck_reason = f"{consecutive_failures} 周続けて失敗しました。"
            else:
                consecutive_failures = 0
                stuck_reason = ""

            # 失敗していなくても、同じ結果が続くなら進んでいない。
            #
            # 実測で6〜10周目がこれだった。集計の抽出が空振りして毎回
            # 「Count: 0, Sum: 0」が返るのに、終了コード0・標準エラーも空。
            # 失敗の signal がどこにも立たないので、上限まで回り切ってしまった。
            # 出力が丸ごと同じなら、書き換えても結果が変わっていないということ。
            output = (result.get("stdout") or "").strip()
            if output and output == last_output:
                repeated_outputs += 1
                if not stuck_reason:
                    stuck_reason = (
                        f"{repeated_outputs + 1} 周続けて同じ結果です。")
            else:
                repeated_outputs = 0
            last_output = output

            if (consecutive_failures >= max_consecutive_failures
                    or repeated_outputs + 1 >= max_consecutive_failures):
                stopped_by = STOP_STUCK
                stop_detail = (stuck_reason
                               + "同じところで足踏みしている可能性が高いので中断します。")
                emit("round_end", round=rounds, reason=stopped_by,
                     elapsed=time.time() - round_started)
                break

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
# 常駐から起こされたときの親 pid。0 なら見張らない(手で叩いたとき)。
PARENT_PID = 0


def spawn(prompt_path=None, watch=False, auto=False, max_rounds=None,
          ps_timeout=None, response_timeout=None, paste_limit=None,
          finish_word="", loop_timeout=None, on_event=None, parent_pid=None,
          approval_timeout=None):
    """このループを別プロセスで起こし、進捗を on_event に流す。(proc, thread) を返す。

    【常駐の中で run_loop を直接呼んではいけない】
    run_loop は UI Automation を使う。常駐は音声切替(pycaw)を持っていて、UIA と
    pycaw を同じプロセスに置くと GC のたびに 0xC0000005 で即死する(実測値は
    copilot_watchdog.py 冒頭)。**スレッドを分けても同じプロセスなら助からない。**
    2026-09-05 に、常駐のワーカースレッドで run_loop を起こした瞬間に落ちた。

    進捗は子の標準出力から1行1件の JSON で受け取る。読み役はテキストを読むだけで
    COM に触らないので、常駐に UIA が入り込む余地が無い。

    on_event はワーカースレッドから呼ばれる。Qt のウィジェットを直接触らないこと
    (シグナル経由でメインスレッドへ渡す)。"""
    exe = sys.executable
    # pythonw.exe には標準出力が無い。進捗を受け取りたいので python.exe を使い、
    # コンソール窓は CREATE_NO_WINDOW で出さないようにする。
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[: -len("pythonw.exe")] + "python.exe"

    argv = [exe, str(_HERE / "agent_loop.py")]
    if prompt_path:
        argv.append(str(prompt_path))
    if watch:
        argv.append("--watch")
    if auto:
        argv.append("--auto")
    argv.append("--emit-events")
    for flag, value in (("--max-rounds", max_rounds), ("--ps-timeout", ps_timeout),
                        ("--response-timeout", response_timeout),
                        ("--paste-limit", paste_limit),
                        ("--loop-timeout", loop_timeout),
                        ("--approval-timeout", approval_timeout)):
        if value is not None:
            argv += [flag, str(value)]
    if finish_word:
        argv += ["--finish-word", finish_word]
    if parent_pid:
        argv += ["--parent-pid", str(parent_pid)]

    proc = subprocess.Popen(
        argv, cwd=str(_HERE),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    def reader():
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue  # JSON でない行(警告など)は捨てる
                if on_event is not None:
                    on_event(payload)
        except Exception as e:  # noqa: BLE001
            print(f"[agent_loop] 出力の読み取りに失敗: {e}", file=sys.stderr)

    thread = threading.Thread(target=reader, name="agent-loop-reader", daemon=True)
    thread.start()
    return proc, thread


def _cancel_requested() -> bool:
    """次の周に進んでよいか。止める理由があれば True。

    キャンセルのフラグに加えて、親(常駐)が消えていないかも見る。常駐が落ちても
    subprocess の子は Windows では生き残るので、見張らないと Copilot に書き込み
    続けることになる。止める手段(トレイのメニュー)は常駐と一緒に消えている。"""
    if CANCEL_FLAG.exists():
        return True
    if PARENT_PID:
        try:
            import psutil
            if not psutil.pid_exists(PARENT_PID):
                return True
        except Exception:  # noqa: BLE001  見張りのために落ちない
            pass
    return False


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
# 人に聞く(承認)
# ---------------------------------------------------------------------------
# 【何のためにあるか】
# ループが止まる理由のうち、危険パターン検出・dry-run・スニペット複数は、
# 「危ないから止めた」ではなく「人に一度見てほしいから止めた」もの。ところが今までは
# そこで完全に終了していたので、見て納得しても最初からやり直しだった。
# ここは、その場で「続けてよい」と答えられるようにするための往復。
#
# 【安全の作法】
# - 答えは1回きり。次に同じ場面が来たらまた聞く。覚えさせない。
# - 答えが来なければ止まる(今までと同じ)。席を外している間に走り出さない。
# - 待っている間もキャンセルは効く。
def answer_approval(token: str, answer: str) -> None:
    """親(常駐)から返事を書く。answer は 'run'(続行) か 'stop'(中止)。

    token は聞いた側が発行した合言葉。古い返事が次の場面に効いてしまわないよう、
    子は token が一致したときだけ受け取る。"""
    try:
        DECISION_FILE.write_text(
            json.dumps({"token": token, "answer": answer}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def _clear_decision() -> None:
    try:
        DECISION_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _read_decision(token: str):
    """自分が聞いた件への返事なら 'run'/'stop' を返す。無ければ None。"""
    try:
        raw = DECISION_FILE.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("token") != token:
        return None
    answer = data.get("answer")
    return answer if answer in ("run", "stop") else None


# _ask_approval の返り値。「続けてよい」以外を一緒くたにしない。
ANSWER_RUN = "run"        # 人が「実行して続行」を選んだ
ANSWER_STOP = "stop"      # 人が「ここで止める」を選んだ
ANSWER_NONE = "none"      # 誰も答えなかった(時間切れ・聞かない設定・停止要求)


def _ask_approval(emit, kind, summary, detail, timeout, poll=0.5):
    """人に「続けてよいか」を聞いて、返事を待つ。ANSWER_* のどれかを返す。

    聞いた事実はイベントで親へ流す。ログ窓がそれを見てボタンを出し、押されたら
    answer_approval() が返事を書く。ここはその返事をポーリングするだけなので、
    ログ窓が開いていなくても(＝誰も答えなくても)時間切れで安全側に倒れる。

    【「止める」と「誰も答えなかった」を区別する理由】
    どちらも続行しない点は同じだが、**後始末が違う**。人が自分で止めたなら、
    その人は理由を分かっている。誰も答えなかったなら、あとで気づく手がかりが要る。
    まとめて False にしていたせいで、自分で止めたのに Copilot の入力欄へ
    「人の判断が必要です」と書き込まれていた(判断はたった今済んでいる)。"""
    if timeout <= 0:
        # 聞く相手が居ない設定。イベントも出さない — 誰も答えられないのに
        # 「返事待ち」がログに残ると、答えそびれたように見えてしまう。
        return ANSWER_NONE
    token = f"{int(time.time() * 1000):x}"
    _clear_decision()
    emit("approval_request", kind=kind, token=token,
         summary=summary, detail=detail, timeout=timeout)
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if _cancel_requested():
            emit("approval_result", kind=kind, token=token, answer="stop",
                 reason="停止要求")
            return ANSWER_NONE
        answer = _read_decision(token)
        if answer is not None:
            _clear_decision()
            emit("approval_result", kind=kind, token=token, answer=answer)
            return ANSWER_RUN if answer == "run" else ANSWER_STOP
        time.sleep(poll)
    _clear_decision()
    emit("approval_result", kind=kind, token=token, answer="stop",
         reason=f"{int(timeout)} 秒返事がありませんでした")
    return ANSWER_NONE


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
    parser.add_argument("--emit-events", action="store_true",
                        help="進捗イベントを1行1件のJSONで標準出力へ流す（常駐が読む）")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="この pid が消えたら次の周の頭で止まる（常駐が指定する）")
    parser.add_argument("--max-consecutive-failures", type=int,
                        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
                        help="実行がこの回数続けて失敗したら中断する")
    parser.add_argument("--take-last", action="store_true",
                        help="監視モードで、新しい応答を待たず画面の最後の応答を引き取る")
    parser.add_argument("--watch-timeout", type=int, default=DEFAULT_WATCH_TIMEOUT,
                        help="監視モードで新しい応答を待つ上限(秒)")
    parser.add_argument("--approval-timeout", type=int, default=None,
                        help="止まる前に人へ聞いて待つ上限(秒)。0 なら聞かずに止まる。"
                             "--emit-events のとき既定 %d、単独実行では既定 0"
                             % DEFAULT_APPROVAL_TIMEOUT)
    args = parser.parse_args(argv)

    # 聞く相手が居るときだけ聞く。--emit-events は常駐がログ窓で受けている印なので、
    # そこには答えられる人が居る。単独で叩いたときは答える口が無いので、待たずに
    # 今までどおり止める(黙って何分も固まるほうが困る)。
    approval_timeout = args.approval_timeout
    if approval_timeout is None:
        approval_timeout = DEFAULT_APPROVAL_TIMEOUT if args.emit_events else 0

    global PARENT_PID
    PARENT_PID = args.parent_pid

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
    # 常駐から別プロセスで起こされたときは、進捗をここから流し返す。
    #
    # 【なぜ常駐の中で回さないのか】
    # run_loop は UI Automation を使う。常駐は音声切替(pycaw)を持っていて、
    # UIA と pycaw を同じプロセスに置くと GC のたびに 0xC0000005 で即死する
    # (copilot_watchdog.py 冒頭に実測値)。実際 2026-09-05 に、常駐のワーカー
    # スレッドで run_loop を起こした瞬間に落ちた。だから別プロセスにして、
    # 進捗だけを標準出力で返す。
    def emit_line(payload):
        try:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()   # 常駐が待っているので溜めない
        except (OSError, ValueError):
            pass

    summary = run_loop(
        on_event=emit_line if args.emit_events else None,
        initial_prompt=prompt, watch=args.watch,
        max_rounds=args.max_rounds, ps_timeout=args.ps_timeout,
        response_timeout=args.response_timeout, paste_limit=args.paste_limit,
        finish_word=args.finish_word, auto_run=args.auto,
        loop_timeout=args.loop_timeout,
        max_consecutive_failures=args.max_consecutive_failures,
        wait_for_new=not args.take_last, watch_timeout=args.watch_timeout,
        approval_timeout=approval_timeout,
    )
    if not args.emit_events:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["stopped_by"] in (STOP_FINISH_WORD, STOP_DRY_RUN,
                                          STOP_NO_SNIPPET) else 1


if __name__ == "__main__":
    sys.exit(main())
