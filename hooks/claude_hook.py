# claude_hook.py
# Claude Code のフックから tray-tools を鳴らすための小さな中継。
#
# 何のためか
# ----------
# Claude Code を回している間、画面を見ていないと次のことに気づけない:
#   - 権限確認で止まっている(承認するまで先へ進まない)
#   - 応答が終わった / 途中で失敗した
# tray-tools には既に beep / pushover / notify の口があるので、フックから叩くだけで
# 「画面を見ていなくても分かる」状態になる。新しい常駐は増やさない。
#
# 使い方(~/.claude/settings.json の hooks から呼ぶ)
# -------------------------------------------------
#   python claude_hook.py start   … UserPromptSubmit。開始時刻を控えるだけ(音は鳴らさない)
#   python claude_hook.py stop    … Stop。掛かった時間が閾値を超えたときだけ鳴らす
#   python claude_hook.py fail    … StopFailure。失敗は時間に関わらず鳴らす
#   python claude_hook.py ask     … Notification(permission_prompt)。確認待ちを知らせる
#
# フックの標準入力には JSON が1つ来る(session_id, cwd, last_assistant_message など)。
# 来なくても・壊れていても動くようにしてある。
#
# 【必ず 0 で終わること】
# Stop フックが終了コード 2 を返すと「停止をブロック」と解釈され、Claude が止まれなく
# なる。音を鳴らすだけのフックでそれが起きるのは事故でしかないので、この中では何が
# あっても 0 で抜ける(理由は標準エラーへ書くだけにする)。
#
# 【なぜ経過時間で絞るのか】
# Stop は毎ターン発火する。相槌のような短い応答でも鳴るので、素で繋ぐと必ず鬱陶しく
# なって数日で外すことになる。既定では 60 秒を超えたときだけ鳴らす。
#
# 【tray-tools が起きていなければ何もしない】
# traytools_send.send() は未起動なら本体を起動して最大6秒待つが、それはあふｗから
# 呼ぶときの作法。フックで数秒止まるのは困るし、音を鳴らすためだけに常駐を起こすのも
# 筋が違う。ここでは _exchange() を直に使い、パイプが無ければ黙って諦める。
#
# 【PySide6 を import しない】
# フックは毎ターン走る。Qt の import だけで1秒近くかかるので、そのぶん全ターンが
# 遅くなる。traytools_send.py と mouse_jiggler.py はどちらも標準ライブラリ(ctypes)
# だけで動くので、この2つまでは読んでよい。
import json
import os
import re
import sys
import time
from pathlib import Path

# 親フォルダ(tray-tools 本体)を import できるようにする。フックはどこから呼ばれるか
# 分からないので、カレントディレクトリには頼らない。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mouse_jiggler  # noqa: E402  (ctypes だけ。副作用なし)
import traytools_send  # noqa: E402  (標準ライブラリだけ)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# 呼ばれるたびに1行残す。フックは目に見えないところで走るので、記録が無いと
# 「鳴らなかった」のが「呼ばれていない」のか「短くて見送った」のか「送ったが
# 聞こえなかった」のか切り分けられない。実際その3つで詰まった。
LOG_PATH = Path(__file__).resolve().parent / "hook.log"

# 記録の上限行数。超えたら古い方から捨てる(放っておくと際限なく伸びる)。
LOG_MAX_LINES = 500

# 状態の置き場所。セッションごとに1ファイル。複数セッションを並行させても混ざらない
# ように、フックの JSON にある session_id をファイル名に使う。
STATE_DIR = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "tray-tools-hooks"

# 置きっぱなしを片付ける閾値(秒)。セッションが落ちると stop が来ないまま残る。
STATE_MAX_AGE = 24 * 60 * 60

DEFAULTS = {
    # 全体の入切。切ってもフック定義を消さずに黙らせられる。
    "enabled": True,
    # これより短い応答では鳴らさない(秒)。Stop は毎ターン飛ぶのでこれが要。
    "min_seconds": 60,
    # 最後のキー/マウス操作からこれだけ経っていたら「離席中」とみなす(秒)。
    "away_seconds": 180,
    # 離席中はスマホへも送るか。
    "pushover_when_away": True,
    # スマホへ送るのはこれ以上かかったときだけ(秒)。音より敷居を上げておく。
    "pushover_min_seconds": 300,
    # スマホの本文に応答の書き出しを載せるか。既定では載せない(どのフォルダの作業が
    # 何分で終わったかだけ送る)。中身は手元の画面で見ればよく、通知に流す必要はない。
    "pushover_include_excerpt": False,
    # 載せる場合の長さ。
    "excerpt_chars": 120,
    # 種類ごとの入切。
    "beep_on_done": True,
    "beep_on_error": True,
    "beep_on_ask": True,
    # どの音を鳴らすか(beep.py の名前: ok / done / warn / error / ask)。
    #
    # 確認待ちに "ask" を使わないのは、Windows の既定のサウンド設定では
    # 「質問(SystemQuestion)」に音が割り当てられておらず、いちばん気づきたい
    # 「止まっている」が**無音になる**ため。実際このPCでも空だった。
    # コントロールパネルのサウンドで「質問」に音を割り当てたなら "ask" に戻してよい。
    "sound_done": "done",
    "sound_error": "error",
    "sound_ask": "warn",
    # 確認待ちだけ複数回鳴らす。既定のサウンド設定では「情報」と「警告」が同じ
    # wav を指しているので、音色では done と区別が付かない。回数で分ける。
    "ask_repeat": 2,
    # 繰り返すときの間隔(秒)。MessageBeep は鳴り終わるのを待たずに戻るので、
    # 間を空けないと2回目が重なって1回に聞こえる。
    "ask_repeat_interval": 0.35,
}

# ファイル名に使ってよい文字。session_id は UUID のはずだが、外から来る値なので
# そのままパスに混ぜない(`..` や `\` が来たら別の場所を書きに行ってしまう)。
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _log(message: str) -> None:
    """理由を標準エラーへ。フックの stdout は Claude 側に解釈されうるので使わない。"""
    print(f"[tray-tools hook] {message}", file=sys.stderr)


def _record(action: str, message: str) -> None:
    """hook.log に1行残す。書けなくても黙って諦める(記録のために本体を止めない)。"""
    try:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {action:5}  {message}"
        with open(LOG_PATH, "a", encoding="utf-8", newline="") as f:
            # print で改行を付ける。newline="" なので LF のまま入る。
            print(line, file=f)
        _trim_log()
    except OSError:
        pass


def _trim_log() -> None:
    """行数が増えすぎたら古い方から捨てる。"""
    try:
        if not LOG_PATH.exists() or LOG_PATH.stat().st_size < 200 * LOG_MAX_LINES:
            return
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > LOG_MAX_LINES:
            LOG_PATH.write_text("".join(lines[-LOG_MAX_LINES:]), encoding="utf-8", newline="")
    except OSError:
        pass


def load_config() -> dict:
    """config.json を読む。無ければ既定のまま。壊れていても既定へ落とす。"""
    config = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(stored, dict):
                config.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError) as e:
        _log(f"config.json を読めません（既定を使います）: {e}")
    return config


def read_event() -> dict:
    """標準入力のフック JSON。無い・壊れているなら空の辞書。

    フックを手で試すときは標準入力が繋がっていないこともあるので、そこで止めない。"""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not (raw or "").strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# セッションごとの開始時刻
# ---------------------------------------------------------------------------
def _state_path(session_id: str):
    name = _SAFE_NAME.sub("_", (session_id or "").strip())[:80]
    if not name:
        return None
    return STATE_DIR / f"{name}.json"


def _sweep_state() -> None:
    """古い状態ファイルを片付ける。セッションが落ちると stop が来ずに残るため。"""
    try:
        if not STATE_DIR.exists():
            return
        limit = time.time() - STATE_MAX_AGE
        for path in STATE_DIR.glob("*.json"):
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


def mark_start(event: dict) -> None:
    """このターンの開始時刻を控える。ここでは音を鳴らさない(毎プロンプト走るため)。"""
    path = _state_path(event.get("session_id"))
    if path is None:
        _record("start", "session_id が無いので控えられません")
        return
    _record("start", f"開始を控えました（{path.name}）")
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"started": time.time(), "cwd": event.get("cwd") or ""}),
            encoding="utf-8",
        )
    except OSError as e:
        _log(f"開始時刻を書けません: {e}")
    _sweep_state()


def take_elapsed(event: dict):
    """開始からの経過秒を返して、控えを捨てる。控えが無ければ None。

    None は「分からない」であって「0秒」ではない。フックを入れた直後や、
    再開したセッションの1ターン目がこれに当たる。分からないときは鳴らさない
    (静かな方に倒す。鳴らしすぎて外されるのがいちばん困る)。"""
    path = _state_path(event.get("session_id"))
    if path is None or not path.exists():
        return None
    try:
        started = json.loads(path.read_text(encoding="utf-8")).get("started")
        path.unlink()
        if not isinstance(started, (int, float)):
            return None
        return max(0.0, time.time() - started)
    except (OSError, ValueError) as e:
        _log(f"開始時刻を読めません: {e}")
        return None


# ---------------------------------------------------------------------------
# tray-tools へ送る
# ---------------------------------------------------------------------------
# 応答を待つ上限(秒)。traytools_send は pushover に最長25秒待つが、こちらは応答を
# 使わないので待つ意味が無い。フックはターンの終わりに走るので、ここが長いとそのまま
# 「終わったのに戻ってこない」時間になる。パイプへ書けた時点で本体は受け取っている。
RESPONSE_TIMEOUT = 2.0


def send(command: str, args: list) -> bool:
    """常駐へ1つ送る。起きていなければ何もしない(起こしには行かない)。"""
    try:
        traytools_send._exchange(command, args, RESPONSE_TIMEOUT)
        return True
    except FileNotFoundError:
        # 待ち受けが無い＝tray-tools は起きていない。黙って諦めるが、記録は残す
        # (「鳴らなかった」の理由でいちばん多いのがこれ)。
        _record("send", f"{command} {args} → tray-tools が起きていません")
        return False
    except OSError as e:
        _log(f"送れませんでした（{command}）: {e}")
        _record("send", f"{command} {args} → 送れません: {e}")
        return False


def beep(config: dict, sound_key: str, times: int = 1, interval: float = 0.0) -> None:
    """設定で指定された音を鳴らす。回数と間隔を指定できる。"""
    kind = config.get(sound_key) or "done"
    for index in range(max(1, int(times))):
        if index:
            time.sleep(max(0.0, float(interval)))
        send("beep", [str(kind)])


def is_away(config: dict) -> bool:
    """最後の操作から離れているか。取得できなければ「居る」に倒す。"""
    idle = mouse_jiggler.idle_seconds()
    if idle is None:
        return False
    return idle >= config["away_seconds"]


def _format_minutes(seconds) -> str:
    if seconds is None:
        return "経過不明"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分"
    return f"{seconds // 3600}時間{(seconds % 3600) // 60}分"


def _project_name(event: dict) -> str:
    cwd = (event.get("cwd") or "").strip()
    return Path(cwd).name if cwd else "Claude Code"


def maybe_pushover(config: dict, event: dict, title: str, body: str, elapsed=None) -> None:
    """離席中のときだけスマホへ。手元に居るなら音で足りる。"""
    if not config["pushover_when_away"] or not is_away(config):
        return
    if elapsed is not None and elapsed < config["pushover_min_seconds"]:
        return
    if config["pushover_include_excerpt"]:
        excerpt = (event.get("last_assistant_message") or "").strip().replace("\n", " ")
        if excerpt:
            body += "\n" + excerpt[: config["excerpt_chars"]]
    send("pushover", [body, "--title", title])


# ---------------------------------------------------------------------------
# 各イベント
# ---------------------------------------------------------------------------
def on_stop(config: dict, event: dict) -> None:
    elapsed = take_elapsed(event)
    if elapsed is None:
        _record("stop", "開始の控えが無いので見送り（フック導入直後 / 再開したセッション）")
        return
    if elapsed < config["min_seconds"]:
        _record("stop", f"経過 {elapsed:.1f}秒 < {config['min_seconds']}秒 なので見送り")
        return
    _record("stop", f"経過 {elapsed:.1f}秒 → {config['sound_done']} を鳴らす")
    if config["beep_on_done"]:
        beep(config, "sound_done")
    maybe_pushover(
        config,
        event,
        _project_name(event),
        f"応答が終わりました（{_format_minutes(elapsed)}）",
        elapsed,
    )


def on_fail(config: dict, event: dict) -> None:
    # 失敗は時間で絞らない。短く終わったということは、たいてい早々に落ちたということ。
    elapsed = take_elapsed(event)
    if config["beep_on_error"]:
        beep(config, "sound_error")
    _record("fail", f"経過 {_format_minutes(elapsed)} → {config['sound_error']} を鳴らす")
    reason = (event.get("error_type") or event.get("error_message") or "").strip()
    body = f"止まりました（{_format_minutes(elapsed)}）"
    if reason:
        body += f"\n{reason[:80]}"
    maybe_pushover(config, event, _project_name(event), body)


def on_ask(config: dict, event: dict) -> None:
    # 確認待ちは、放っておくといつまでも進まない。経過時間では絞らない。
    _record("ask", f"{config['sound_ask']} を {config['ask_repeat']} 回鳴らす")
    if config["beep_on_ask"]:
        beep(config, "sound_ask", config["ask_repeat"], config["ask_repeat_interval"])
    maybe_pushover(config, event, _project_name(event), "確認待ちで止まっています")


ACTIONS = {
    "start": lambda _config, event: mark_start(event),
    "stop": on_stop,
    "fail": on_fail,
    "ask": on_ask,
}


def main() -> int:
    """何があっても 0 を返す。Stop フックの 2 は「停止をブロック」の意味になる。"""
    try:
        action = sys.argv[1] if len(sys.argv) > 1 else ""
        handler = ACTIONS.get(action)
        if handler is None:
            _log(f"知らない指示です: {action!r}（{'/'.join(ACTIONS)} のどれか）")
            return 0
        config = load_config()
        if not config["enabled"]:
            return 0
        handler(config, read_event())
    except Exception as e:  # noqa: BLE001  フックで落ちても本体を止めない
        _log(f"想定外の失敗: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
