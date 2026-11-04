# claude_status.py
# Claude Code の各セッションが「いまどうなっているか」を、フックと常駐の間で受け渡す。
#
# 書くのは hooks/claude_hook.py(フック。毎ターン・ツール呼び出しごとに走る)、
# 読むのは feature_screen.py(トレイの輪・ツールチップ・メニュー)。どちらからも読むので
# 標準ライブラリだけで書く(フックは PySide6 を読まない。CLAUDE.md)。
#
# 置き場所はセッションごとに1ファイル(%TEMP%\tray-tools-hooks\status\<session_id>.json)。
# 開始時刻の控え(hooks の STATE_DIR 直下)とは分ける。あちらは stop で消すが、こちらは
# 「完了」を見せるために残す。
#
# 状態は4つ: running(実行中) / ask(確認待ち) / done(完了) / fail(失敗)。
# Claude Code は応答が終わった後は何も知らせてこないので、完了・失敗は一定時間で
# 一覧から外す(DONE_TTL)。走っているはずが長く音沙汰が無いもの(アプリごと閉じた等)も外す。
import json
import os
import re
import time
from pathlib import Path

STATUS_DIR = (Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
              / "tray-tools-hooks" / "status")

RUNNING, ASK, DONE, FAIL = "running", "ask", "done", "fail"

# 完了・失敗を一覧に残す時間(秒)。
DONE_TTL = 30 * 60
# 実行中・確認待ちのまま、これだけ何も起きなければ見えなくする(秒)。
# 1ターンがこれより長いことはまず無く、あってもフックが途中で何度か書く。
STALE_TTL = 6 * 60 * 60
# ファイルそのものを消す時間(秒)。
FILE_MAX_AGE = 24 * 60 * 60

LABELS = {RUNNING: "実行中", ASK: "確認待ち", DONE: "完了", FAIL: "失敗"}
# トレイの輪に出す優先順(大きいほど強い)。止まって待っているのがいちばん見落としたくない。
PRIORITY = {ASK: 4, FAIL: 3, RUNNING: 2, DONE: 1}

# 計画を書くツール。デスクトップアプリの右のタスク一覧と、CLI の TodoWrite。
PLAN_TOOLS = ("mcp__ccd_turn__update_plan", "TodoWrite")

# session_id は外から来る値なので、そのままパスに混ぜない(hooks の _SAFE_NAME と同じ)。
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def status_path(session_id, directory=None):
    name = _SAFE_NAME.sub("_", str(session_id or "").strip())[:80]
    if not name:
        return None
    return Path(directory or STATUS_DIR) / f"{name}.json"


def read(session_id, directory=None) -> dict:
    path = status_path(session_id, directory)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(session_id, data: dict, directory=None) -> bool:
    """一時ファイルに書いてから置き換える(常駐が書きかけを読まないように)。"""
    path = status_path(session_id, directory)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 一時ファイル名にプロセス番号を入れる。PostToolUse は async で並んで走ることが
        # あり、同じ名前だと互いの書きかけを置き換え合う。
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# デスクトップアプリのセッション ID。アプリ本体の受け口(claude://code/continue)が
# 受け付ける形(local_ に英数字とハイフン)と同じ条件で確かめる。外から来る値なので、
# この形でなければリンクに混ぜない。
_HOST_SESSION = re.compile(r"^local_[A-Za-z0-9-]{1,64}$")


def valid_host_session(value) -> bool:
    return bool(_HOST_SESSION.match(str(value or "")))


def jump_url(entry: dict) -> str:
    """そのセッションをデスクトップアプリで開くリンク。アプリのセッションでなければ空。"""
    host = (entry or {}).get("host_session_id") or ""
    if not valid_host_session(host):
        return ""
    return f"claude://code/continue?session={host}"


def open_in_app(entry: dict) -> bool:
    """デスクトップアプリでそのセッションを開く。開けなければ False。"""
    url = jump_url(entry)
    if not url:
        return False
    try:
        os.startfile(url)
        return True
    except OSError:
        return False


def plan_fraction(plan):
    """バーの長さ(0〜1)。計画が無ければ None(長さを出せない＝流れる帯にする)。

    進行中の手順は半分済んだものとして数える(3/6 の最中なら 2.5/6)。始めたばかりで
    0 のまま止まって見えるのを避けるため。"""
    plan = [t for t in (plan or []) if isinstance(t, dict)]
    if not plan:
        return None
    done = sum(1 for t in plan if t.get("status") == "completed")
    doing = sum(1 for t in plan if t.get("status") == "in_progress")
    return min(1.0, (done + 0.5 * doing) / len(plan))


# 同じフォルダで複数のセッションを開くと名前だけでは見分けられないので、
# 最後に送ったメッセージの書き出しを添える(「claude「私の方でなにか…」」)。
TITLE_CHARS = 10
MENU_TITLE_CHARS = 24
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


def prompt_title(prompt) -> str:
    """送ったメッセージから、見分けるための短い書き出しを作る。タグ(貼り付けの囲みなど)は除く。"""
    text = _SPACE.sub(" ", _TAG.sub(" ", str(prompt or ""))).strip()
    return text


# デスクトップアプリが保存しているセッションの情報。サイドバーに出ている名前(title)が
# 入っている。アプリ内部の保存形式で公開された仕様ではないので、場所や項目が変わったら
# 名前が出ないだけにする(フォルダ名「書き出し」の表示に戻る)。
APP_SESSIONS_DIR = (Path(os.environ.get("APPDATA") or Path.home())
                    / "Claude" / "claude-code-sessions")
# アプリの名前は書き出しより長めに見せる(陽太さんが付けた名前を途中で切りすぎない)。
APP_NAME_EXTRA = 14
_app_paths = {}     # local_… → ファイルの場所
_app_titles = {}    # 場所 → (更新時刻, 名前)。更新されたときだけ読み直す(1つ40KBほど)


def app_session_title(host_session_id, directory=None) -> str:
    """デスクトップアプリでのセッション名。取れなければ空。"""
    if not valid_host_session(host_session_id):
        return ""
    root = Path(directory or APP_SESSIONS_DIR)
    key = (str(root), host_session_id)
    path = _app_paths.get(key)
    try:
        if path is None or not path.exists():
            # <アカウント>/<組織>/local_….json。ID は確かめ済みなのでパターンに混ぜてよい。
            found = next(iter(root.glob(f"*/*/{host_session_id}.json")), None)
            if found is None:
                return ""
            path = _app_paths[key] = found
        mtime = path.stat().st_mtime
        cached = _app_titles.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
        title = _SPACE.sub(" ", title)
        _app_titles[path] = (mtime, title)
        return title
    except (OSError, ValueError):
        return ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def display_name(entry: dict, limit: int = TITLE_CHARS) -> str:
    """見せる名前。アプリのセッション名があればそれ、無ければ「フォルダ名「書き出し」」。"""
    app_title = app_session_title(entry.get("host_session_id"))
    if app_title:
        return _clip(app_title, limit + APP_NAME_EXTRA)
    name = entry.get("project") or "Claude Code"
    title = str(entry.get("title") or "").strip()
    if not title:
        return name
    return f"{name}「{_clip(title, limit)}」"


def project_name(cwd) -> str:
    cwd = str(cwd or "").strip().rstrip("\\/")
    return Path(cwd).name if cwd else "Claude Code"


# ---------------------------------------------------------------------------
# 計画(タスク一覧)
# ---------------------------------------------------------------------------
def apply_plan(plan, tool_name: str, tool_input) -> list:
    """計画ツールの呼び出し1回を、手元の計画([{content, status}])に当てる。

    update_plan は毎回全体を送るとは限らない({"advance": true} や
    {"done": [2], "start": 4} だけのこともある)ので、前の計画に差分を当てて保つ。"""
    plan = [dict(item) for item in (plan or []) if isinstance(item, dict)]
    data = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "TodoWrite":
        todos = data.get("todos")
        if isinstance(todos, list):
            return [{"content": str(t.get("content") or ""), "status": str(t.get("status") or "")}
                    for t in todos if isinstance(t, dict)]
        return plan
    items = data.get("items")
    if isinstance(items, list):
        plan = [{"content": str(t.get("content") or ""), "status": str(t.get("status") or "")}
                for t in items if isinstance(t, dict)]
    if data.get("advance"):
        current = next((i for i, t in enumerate(plan) if t["status"] == "in_progress"), None)
        if current is not None:
            plan[current]["status"] = "completed"
            following = next((i for i in range(current + 1, len(plan))
                              if plan[i]["status"] == "pending"), None)
            if following is not None:
                plan[following]["status"] = "in_progress"
    for number in data.get("done") or []:
        if isinstance(number, int) and 1 <= number <= len(plan):
            plan[number - 1]["status"] = "completed"
    start = data.get("start")
    if isinstance(start, int) and 1 <= start <= len(plan):
        for item in plan:
            if item["status"] == "in_progress":
                item["status"] = "pending"
        plan[start - 1]["status"] = "in_progress"
    return plan


def plan_text(plan) -> str:
    """「3/6 テストと実常駐で確認」。進行中が無ければ「4/6 済」。計画が無ければ空。"""
    plan = [t for t in (plan or []) if isinstance(t, dict)]
    if not plan:
        return ""
    for index, item in enumerate(plan, 1):
        if item.get("status") == "in_progress":
            return f"{index}/{len(plan)} {item.get('content') or ''}".strip()
    finished = sum(1 for t in plan if t.get("status") == "completed")
    return f"{finished}/{len(plan)} 済"


# ---------------------------------------------------------------------------
# 一覧(トレイ側)
# ---------------------------------------------------------------------------
def visible(entry: dict, now: float) -> bool:
    state = entry.get("state")
    updated = entry.get("updated") or 0
    if state in (DONE, FAIL):
        return now - (entry.get("ended") or updated) < DONE_TTL
    if state in (RUNNING, ASK):
        return now - updated < STALE_TTL
    return False


def list_sessions(directory=None, now=None) -> list:
    """見せるセッションを、強い状態から(同じなら新しい順に)並べて返す。"""
    now = time.time() if now is None else now
    directory = Path(directory or STATUS_DIR)
    entries = []
    try:
        paths = list(directory.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and visible(data, now):
            data["_path"] = str(path)
            entries.append(data)
    entries.sort(key=lambda e: (-PRIORITY.get(e.get("state"), 0), -(e.get("updated") or 0)))
    return entries


def overall_state(entries):
    """トレイの輪に出す状態(いちばん強いもの)。何も無ければ None。"""
    return entries[0].get("state") if entries else None


def format_duration(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60:02d}秒" if seconds < 600 else f"{seconds // 60}分"
    return f"{seconds // 3600}時間{(seconds % 3600) // 60}分"


def describe(entry: dict, now=None) -> list:
    """ツールチップ用の行。[1行目(プロジェクトと状態の詳細), 2行目(計画、あれば)]。"""
    now = time.time() if now is None else now
    name = display_name(entry)
    state = entry.get("state")
    if state == RUNNING:
        head = f"{name}　{format_duration(now - (entry.get('started') or now))}"
    elif state == ASK:
        head = f"{name}　承認を待っています（{format_duration(now - (entry.get('asked') or now))}）"
    elif state == DONE:
        took = (entry.get("ended") or now) - (entry.get("started") or entry.get("ended") or now)
        head = (f"{name}　{format_duration(took)}で完了"
                f"（{time.strftime('%H:%M', time.localtime(entry.get('ended') or now))}）")
        if background_count(entry, now):
            head += "・裏で作業中"
    else:
        head = f"{name}　失敗しました"
    lines = [head]
    plan = plan_text(entry.get("plan"))
    if plan and state in (RUNNING, ASK):
        lines.append(plan)
    return lines


def menu_label(entry: dict, now=None) -> str:
    """メニューの1行。「確認待ち　tray-tools　2分」。"""
    now = time.time() if now is None else now
    state = entry.get("state")
    name = display_name(entry, MENU_TITLE_CHARS)
    if state in (DONE, FAIL):
        tail = time.strftime("%H:%M", time.localtime(entry.get("ended") or now))
    elif state == ASK:
        tail = format_duration(now - (entry.get("asked") or now))
    else:
        tail = format_duration(now - (entry.get("started") or now))
    label = LABELS.get(state, state)
    if background_count(entry, now):
        label += "（裏で作業中）"
    return f"{label}　{name}　{tail}"


def remove(entry: dict, directory=None) -> bool:
    """1つのセッションの状態ファイルを消す(進捗パネルの ✕)。

    パスは一覧が付けた _path ではなく session_id から作り直す(外から来た値で
    任意のファイルを消さないように)。そのセッションが次に動けば、フックがまた書く。"""
    path = status_path((entry or {}).get("session_id"), directory)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 裏で動いている作業(応答は終わったが、裏で走らせた処理がまだ終わっていない)
# ---------------------------------------------------------------------------
# 応答を終えた後も裏の処理が残っていると、終わったときにアプリが Claude を起こして続きを
# させる。その間は Claude が何もしていないのでフックには何も来ない。そこで応答の終わり
# (Stop)に会話の記録(transcript_path)を見て、走らせたまま終わっていない処理を数える。
#   走らせた: ツール結果の "backgroundTaskId":"…"
#   終わった: 終わりの知らせ <task-id>…</task-id> か、TaskStop で止めたもの
# 「完了」の補足として出すだけ(輪の色・トーストは変えない)。起こされないまま終わった
# ときに残り続けないよう BACKGROUND_TTL で外す。
BACKGROUND_TTL = 15 * 60
_BG_STARTED = re.compile(r'"backgroundTaskId":"([A-Za-z0-9_-]+)"')
_BG_NOTIFIED = re.compile(r"<task-id>([A-Za-z0-9_-]+)</task-id>")
_BG_STOPPED = re.compile(r'"name":"TaskStop","input":\{"task_id":"([A-Za-z0-9_-]+)"')


def pending_background(transcript_text: str) -> list:
    """記録の中で、走らせたまま終わりの知らせが来ていない処理の ID。"""
    started = _BG_STARTED.findall(transcript_text or "")
    finished = set(_BG_NOTIFIED.findall(transcript_text or ""))
    finished |= set(_BG_STOPPED.findall(transcript_text or ""))
    seen, pending = set(), []
    for task in started:
        if task not in finished and task not in seen:
            seen.add(task)
            pending.append(task)
    return pending


def pending_background_in(transcript_path) -> list:
    """会話の記録ファイルから数える。読めなければ空(「裏の作業なし」に倒す)。"""
    if not transcript_path:
        return []
    try:
        data = Path(transcript_path).read_bytes()
    except OSError:
        return []
    return pending_background(data.decode("utf-8", errors="replace"))


def background_count(entry: dict, now=None) -> int:
    """「完了（裏で作業中）」として見せる数。完了でない・古いものは 0。"""
    now = time.time() if now is None else now
    if (entry or {}).get("state") != DONE:
        return 0
    count = entry.get("background") or 0
    ended = entry.get("ended") or entry.get("updated") or 0
    if not isinstance(count, int) or count <= 0 or now - ended > BACKGROUND_TTL:
        return 0
    return count


def clear_finished(directory=None) -> int:
    """完了・失敗のファイルを消す(メニューの「完了した行を消す」)。消した数。"""
    count = 0
    directory = Path(directory or STATUS_DIR)
    try:
        paths = list(directory.glob("*.json"))
    except OSError:
        return 0
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("state") in (DONE, FAIL):
                path.unlink()
                count += 1
        except (OSError, ValueError):
            continue
    return count


def sweep(directory=None, now=None) -> None:
    """一日以上前のファイルを消す(フックの start のたびに呼ぶ)。"""
    now = time.time() if now is None else now
    directory = Path(directory or STATUS_DIR)
    try:
        paths = list(directory.glob("*.json")) + list(directory.glob("*.tmp"))
    except OSError:
        return
    for path in paths:
        try:
            if now - path.stat().st_mtime > FILE_MAX_AGE:
                path.unlink()
        except OSError:
            continue
