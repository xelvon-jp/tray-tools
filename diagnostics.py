# diagnostics.py
# 「🩺 診断」のまとめを作る。Qt には依存しない(窓は diagnostics_window.py)。
#
# 何のためにあるか
# ----------------
# 業務PCでは tray-tools が「結構な頻度で落ちる」が、このPCでは落ちなくなっている。
# 原因を追いたいが、業務PCからはデータを持ち出せない(画面を読み上げるのが基本)。
# そこで、落ちた手掛かりになるもの(版・抜けているファイル・環境・Windows の異常終了の
# 記録・crash.log・error.log・起動回数・有効な機能)を短いテキストにまとめ、窓に
# テキストと QR コードで出す。スマホのカメラで読み取って、そのままチャットに貼れる。
#
# 書き方の約束
# ------------
# - **ASCII 中心の短い行。** スマホで読み、チャットに貼る。1項目1行、見出しは短く。
# - **伏せ字は最後に全文へかける**(redact)。各項目で個別に気を付けるだけだと、
#   どこか1か所の漏れがそのまま外へ出る。ホームのパスは ~、他のパスはファイル名だけ、
#   ユーザー名・コンピュータ名・ドメイン名は *、メールアドレスと IP アドレスも伏せる。
# - **秘密情報はそもそも読まない。** Pushover のトークン(資格情報マネージャ)には触れず、
#   settings.json も読むのはオンオフと件数だけ。値そのものは出さない。
# - **期待するファイル一覧をコードに持たない。** 足すたびに直す表は必ず古くなる。
#   代わりに import を ast で静的に辿り、実ファイルが無いものを「抜け」とする
#   (業務PCは以前 ZIP の差分上書きで新規ファイルが抜けたことがある)。
# - 項目ごとに読めなくても、他の項目は出す。業務PCではイベントログの読み取りが
#   権限やポリシーで拒まれることがあり、そのときは「読めず」とだけ書く。
#
# 純関数(parse_* / summarize_* / redact / split_pages など)は tests/test_diagnostics.py が
# 合成した入力で確かめる。実物を読むのは collect() だけ。
#
# 手で動かす(窓を出さずに、まとめを標準出力へ):
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe diagnostics.py
#   ... diagnostics.py --pages     QR に載せるページ単位で出す
import ast
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
CRASH_LOG_PATH = REPO_DIR / "crash.log"
ERROR_LOG_PATH = REPO_DIR / "error.log"
SETTINGS_PATH = REPO_DIR / "settings.json"

# 各ログから出す件数の既定。QR 1〜2枚に収めるための値。
DEFAULT_COUNT = 6
# イベントログを遡る日数。
EVENT_DAYS = 30
# イベントログから取ってくる件数の上限。python 以外(他のアプリ)も混ざるので、出す件数より
# 多めに取ってから絞る。
EVENT_FETCH = 300
# 子プロセス(wevtutil / PowerShell / git)を待つ上限(秒)。業務PCでは数秒かかることがある。
EVENT_TIMEOUT = 25
# crash.log / error.log は末尾だけ読む。何年分も溜まっても読む量が増えないように。
LOG_TAIL_BYTES = 512 * 1024
# 起動見出しから異常終了のプロセスを tray-tools と見なす幅(秒)。プロセスが作られてから
# main() の先頭で見出しを書くまでに、PySide6 などの import が挟まる。遅いPCでも収まる幅。
START_MATCH_SECONDS = 90

# QR 1枚に載せる上限(UTF-8 のバイト数)。誤り訂正 M・バイトモードで version 25 の
# 容量が 997 バイト。画面から読むので version を上げすぎない(25 で 117 セル角)。
# 改ページの印のぶん余裕を持たせる。tests/test_diagnostics.py が version を確かめる。
PAGE_LIMIT_BYTES = 960
QR_ERROR = "m"
QR_MAX_VERSION = 25
QR_BORDER = 4

PAGE_MARK = "[tray-tools diag {page}/{total}]"

# faulthandler が「Windows fatal exception」として書くが、処理されて続行できることが
# ある例外。0x8001010d(RPC_E_CANTCALLOUT_ININPUTSYNCCALL)などの HRESULT(0x8…)と、
# C++ 例外(0xE06D7363 などの 0xE…)は、捕まえる側が居れば落ちない。
# これらは「それだけでは異常終了とは限らない」印(?)を付ける。
HANDLED_CODE_PREFIXES = ("0x8", "0xe")

# 起動見出しは main._install_crash_log が書く。
START_HEADING_RE = re.compile(r"^===== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) 起動 =====\s*$")
# error.log の見出しは main.log_exception / _install_excepthook と capture_process・bg_remove が書く。
ERROR_HEADING_RE = re.compile(r"^===== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*?) =====\s*$")
FRAME_RE = re.compile(r'^\s+File "(.+?)", line (\d+) in (.+?)\s*$')
THREAD_RE = re.compile(r"^(Current thread|Thread) 0x[0-9a-fA-F]+(?: \[(.*?)\])? \(most recent call first\):")
EXC_LINE_RE = re.compile(r"^([A-Za-z_][\w.]*)(?:: (.*))?$")

# 版を出す依存。値は importlib.metadata の配布名(候補を順に試す)。
PACKAGES = [
    ("PySide6", ("PySide6",)),
    ("pycaw", ("pycaw",)),
    ("comtypes", ("comtypes",)),
    ("keyboard", ("keyboard",)),
    ("mss", ("mss",)),
    ("numpy", ("numpy",)),
    ("cv2", ("opencv-python-headless", "opencv-python", "opencv-contrib-python")),
    ("rembg", ("rembg",)),
    ("vtracer", ("vtracer",)),
    ("segno", ("segno",)),
    ("psutil", ("psutil",)),
]


# ===============================================================
# 版(.git を自前で読む)
# ===============================================================
def _git_dir(repo_dir: Path):
    """.git の場所。worktree などで .git がファイル(gitdir: …)のときも辿る。"""
    dot = Path(repo_dir) / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        try:
            text = dot.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:"):].strip())
            if not target.is_absolute():
                target = Path(repo_dir) / target
            return target if target.is_dir() else None
    return None


def _resolve_ref(git_dir: Path, ref: str):
    """refs/heads/main などをコミット ID にする。ばらの ref → packed-refs の順。"""
    loose = git_dir / ref
    try:
        if loose.is_file():
            value = loose.read_text(encoding="utf-8", errors="replace").strip()
            if value.startswith("ref:"):
                return _resolve_ref(git_dir, value[4:].strip())
            if re.fullmatch(r"[0-9a-f]{40}", value):
                return value
    except OSError:
        pass
    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in packed.splitlines():
        if not line or line[0] in "#^":
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == ref and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            return parts[0]
    return None


def _commit_time_from_body(body: bytes):
    match = re.search(rb"^committer .*? (\d+) ([+-]\d{4})$", body, re.M)
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)))


def _read_loose_commit(git_dir: Path, sha: str):
    path = git_dir / "objects" / sha[:2] / sha[2:]
    try:
        data = zlib.decompress(path.read_bytes())
    except (OSError, zlib.error):
        return None
    head, _, body = data.partition(b"\0")
    if not head.startswith(b"commit "):
        return None
    return _commit_time_from_body(body)


def _read_packed_commit(git_dir: Path, sha: str):
    """pack の中のコミットを読む(idx v2 のみ)。差分(delta)で入っていたら諦める。

    コミットはふつう差分にならないので、これで足りる。読めなくても版の ID は出せる。"""
    want = bytes.fromhex(sha)
    pack_dir = git_dir / "objects" / "pack"
    try:
        idx_files = sorted(pack_dir.glob("*.idx"))
    except OSError:
        return None
    for idx_path in idx_files:
        try:
            idx = idx_path.read_bytes()
        except OSError:
            continue
        if idx[:8] != b"\xfftOc\x00\x00\x00\x02":
            continue
        fanout = struct.unpack(">256I", idx[8:8 + 1024])
        total = fanout[255]
        lo = fanout[want[0] - 1] if want[0] else 0
        hi = fanout[want[0]]
        names_at = 8 + 1024
        found = None
        while lo < hi:
            mid = (lo + hi) // 2
            name = idx[names_at + mid * 20:names_at + mid * 20 + 20]
            if name == want:
                found = mid
                break
            if name < want:
                lo = mid + 1
            else:
                hi = mid
        if found is None:
            continue
        offsets_at = names_at + total * 20 + total * 4
        offset = struct.unpack(">I", idx[offsets_at + found * 4:offsets_at + found * 4 + 4])[0]
        if offset & 0x80000000:
            large_at = offsets_at + total * 4 + (offset & 0x7FFFFFFF) * 8
            offset = struct.unpack(">Q", idx[large_at:large_at + 8])[0]
        pack_path = idx_path.with_suffix(".pack")
        try:
            with open(pack_path, "rb") as f:
                f.seek(offset)
                chunk = f.read(64 * 1024)
        except OSError:
            return None
        byte = chunk[0]
        obj_type = (byte >> 4) & 7
        pos = 1
        while byte & 0x80:
            byte = chunk[pos]
            pos += 1
        if obj_type != 1:  # 1 = commit。6/7 は差分
            return None
        try:
            body = zlib.decompressobj().decompress(chunk[pos:])
        except zlib.error:
            return None
        return _commit_time_from_body(body)
    return None


def read_git_version(repo_dir: Path) -> dict:
    """{"state": "git"|"nogit"|"broken", "sha", "branch", "date"}。git コマンドは使わない
    (業務PCに入っていないことがある)。"""
    git_dir = _git_dir(Path(repo_dir))
    if git_dir is None:
        return {"state": "nogit"}
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return {"state": "broken"}
    branch = None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        branch = ref.rsplit("/", 1)[-1]
        sha = _resolve_ref(git_dir, ref)
    elif re.fullmatch(r"[0-9a-f]{40}", head):
        sha = head
    else:
        sha = None
    if sha is None:
        return {"state": "broken", "branch": branch}
    date = _read_loose_commit(git_dir, sha) or _read_packed_commit(git_dir, sha)
    return {"state": "git", "sha": sha, "branch": branch, "date": date}


def summarize_version(info: dict) -> str:
    state = info.get("state")
    if state == "nogit":
        return "ver git なし"
    if state != "git":
        return f"ver .git broken branch={info.get('branch') or '?'}"
    date = info.get("date")
    stamp = date.strftime("%Y-%m-%d %H:%M") if date else "date?"
    branch = info.get("branch") or "detached"
    return f"ver {info['sha'][:7]} {stamp} {branch}"


# ===============================================================
# ファイルの抜けと古さ(import を静的に辿る)
# ===============================================================
_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for node in types:
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        if name in _IMPORT_ERRORS:
            return True
    return False


def _collect_imports(tree) -> list:
    """(名前, 任意か) の一覧。try … except ImportError の中の import は「任意」とする
    (無くても動くように書いてある依存。rembg など)。"""
    found = []

    def visit(node, optional):
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            guarded = optional or any(_catches_import_error(h) for h in node.handlers)
            for child in node.body:
                visit(child, guarded)
            for child in node.handlers + node.orelse + node.finalbody:
                visit(child, optional)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], optional))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.append((node.module.split(".")[0], optional))
        for child in ast.iter_child_nodes(node):
            visit(child, optional)

    visit(tree, False)
    return found


def _default_is_installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def scan_files(repo_dir: Path, is_installed=None) -> dict:
    """リポジトリ直下と tools/ の .py を ast で読み、解決できない import を「抜け」とする。

    返り値は {"count", "newest": (名前, 日時) | None, "missing": [(名前, 読んでいるファイル)],
    "broken": [構文として読めないファイル]}。
    構文として読めないファイルも挙げる(コピーの途中で切れたファイルはこう見える)。"""
    repo_dir = Path(repo_dir)
    is_installed = is_installed or _default_is_installed
    files = sorted(repo_dir.glob("*.py")) + sorted((repo_dir / "tools").glob("*.py"))
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names)
    newest = None
    missing = {}
    broken = []
    for path in files:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if newest is None or mtime > newest[1]:
                newest = (path.name, mtime)
            tree = ast.parse(path.read_bytes(), filename=path.name)
        except (OSError, SyntaxError, ValueError):
            broken.append(path.name)
            continue
        for name, optional in _collect_imports(tree):
            if optional or name in stdlib or name == "__future__":
                continue
            local = any((d / f"{name}.py").is_file() or (d / name).is_dir()
                        for d in {repo_dir, path.parent})
            if local or is_installed(name):
                continue
            missing.setdefault(name, path.name)
    return {"count": len(files), "newest": newest,
            "missing": sorted(missing.items()), "broken": sorted(broken)}


def summarize_files(info: dict) -> list:
    lines = []
    newest = info.get("newest")
    if newest:
        lines.append(f"src {info['count']} files, newest {newest[1]:%Y-%m-%d %H:%M} {newest[0]}")
    else:
        lines.append(f"src {info.get('count', 0)} files")
    missing = info.get("missing") or []
    lines.append("lost " + (" ".join(f"{n}({f})" for n, f in missing) if missing else "none"))
    if info.get("broken"):
        lines.append("broken " + " ".join(info["broken"]))
    return lines


# ===============================================================
# 環境
# ===============================================================
def _windows_build() -> str:
    try:
        ver = sys.getwindowsversion()
    except AttributeError:
        return platform.platform()
    text = f"{ver.major}.{ver.minor}.{ver.build}"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            try:
                text += "." + str(winreg.QueryValueEx(key, "UBR")[0])
            except OSError:
                pass
            try:
                text += " " + str(winreg.QueryValueEx(key, "DisplayVersion")[0])
            except OSError:
                pass
    except (ImportError, OSError):
        pass
    return text


def package_versions() -> list:
    """(表示名, 版 | None)。import はしない(重いものがある・副作用を避ける)。"""
    result = []
    for label, dists in PACKAGES:
        version = None
        for dist in dists:
            try:
                version = importlib.metadata.version(dist)
                break
            except importlib.metadata.PackageNotFoundError:
                continue
            except Exception:  # noqa: BLE001  壊れたメタデータで全体を止めない
                continue
        result.append((label, version))
    return result


def summarize_environment(win: str, python: str, packages: list) -> list:
    lines = [f"os win {win}", f"py {python}"]
    # 版は3つ目まで(4.12.0.88 → 4.12.0)。調べるにはそれで足り、4つ並ぶと IP アドレスの
    # 伏せ字に掛かって読めなくなる。
    parts = [f"{name} {'.'.join(version.split('.')[:3]) if version else 'なし'}"
             for name, version in packages]
    # 1行が長くなりすぎないよう、4つずつ折る。
    for i in range(0, len(parts), 4):
        lines.append("pkg " + " ".join(parts[i:i + 4]))
    return lines


# ===============================================================
# Windows のイベントログ(Application 1000 / 1002)
# ===============================================================
# 日本語版の Windows では、メッセージ本文の項目名が日本語になる。名前付きの Data が
# 取れないとき(古い形式・PowerShell 経由など)は本文から読むので、両方の言い方を持つ。
_MESSAGE_PATTERNS = {
    "app": r"(?:Faulting application name|障害が発生しているアプリケーション名)\s*[:：]\s*([^,、\s]+)",
    "module": r"(?:Faulting module name|障害が発生したモジュール名|障害が発生しているモジュール名)\s*[:：]\s*([^,、\s]+)",
    "code": r"(?:Exception code|例外コード)\s*[:：]\s*(?:0x)?([0-9a-fA-F]{8})",
    "app_path": r"(?:Faulting application path|障害が発生しているアプリケーション\s*パス|Faulting アプリケーション パス)\s*[:：]\s*(.+)",
    "module_path": r"(?:Faulting module path|障害が発生しているモジュール\s*パス|Faulting モジュール パス)\s*[:：]\s*(.+)",
    "hang_app": r"(?:The program|プログラム)\s+(\S+?\.exe)",
}
# 名前の無い Data が並んでいるときの順番(EventID ごと)。
_POSITIONAL = {
    1000: ["AppName", "AppVersion", "AppTimeStamp", "ModuleName", "ModuleVersion",
           "ModuleTimeStamp", "ExceptionCode", "FaultingOffset", "ProcessId",
           "ProcessCreationTime", "AppPath", "ModulePath"],
    1002: ["AppName", "AppVersion", "ProcessId", "StartTime", "TerminationTime",
           "ExeFileName"],
}


def _filetime_to_local(value):
    """FILETIME(16進文字列でも数でも)を、このPCのローカル時刻(naive)にする。"""
    try:
        number = int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    try:
        utc = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=number // 10)
        return utc.astimezone().replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _utc_text_to_local(text: str):
    match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", text or "")
    if not match:
        return None
    utc = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return utc.astimezone().replace(tzinfo=None)


def _xml_unescape(text: str) -> str:
    return (text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&#13;", "\r").replace("&#10;", "\n")
            .replace("&amp;", "&"))


def parse_event_xml(text: str) -> list:
    """wevtutil(/f:xml・/f:RenderedXml)か Get-WinEvent の ToXml() の出力を読む。

    根の要素が無く <Event> が並ぶだけの形なので、1件ずつ正規表現で切り出す
    (ElementTree に渡すと名前空間と根の無さで面倒が増える)。python を含むものだけ返す。
    各要素は {"time", "id", "app", "module", "code", "paths", "created", "hang"}。"""
    events = []
    for raw in re.findall(r"<Event[\s>].*?</Event>", text or "", re.S):
        if "python" not in raw.lower():
            continue
        id_match = re.search(r"<EventID[^>]*>(\d+)</EventID>", raw)
        event_id = int(id_match.group(1)) if id_match else 0
        time_match = re.search(r"<TimeCreated SystemTime=['\"]([^'\"]+)['\"]", raw)
        data = {}
        positional = []
        for name, value in re.findall(r"<Data(?: Name=['\"](\w+)['\"])?\s*(?:/>|>(.*?)</Data>)", raw, re.S):
            value = _xml_unescape(value or "")
            if name:
                data[name] = value
            else:
                positional.append(value)
        if not data and positional:
            for key, value in zip(_POSITIONAL.get(event_id, []), positional):
                data[key] = value
        message_match = re.search(r"<Message>(.*?)</Message>", raw, re.S)
        message = _xml_unescape(message_match.group(1)) if message_match else ""

        def from_message(key):
            match = re.search(_MESSAGE_PATTERNS[key], message)
            return match.group(1).strip() if match else ""

        app = data.get("AppName") or from_message("app") or from_message("hang_app")
        module = data.get("ModuleName") or from_message("module")
        code = (data.get("ExceptionCode") or from_message("code")).lower()
        code = code[2:] if code.startswith("0x") else code
        paths = [p for p in (
            data.get("AppPath") or from_message("app_path"),
            data.get("ModulePath") or from_message("module_path"),
            data.get("ExeFileName", ""),
        ) if p]
        created = _filetime_to_local(data.get("ProcessCreationTime") or data.get("StartTime"))
        events.append({
            "time": _utc_text_to_local(time_match.group(1)) if time_match else None,
            "id": event_id,
            "app": app,
            "module": module,
            "code": code,
            "paths": paths,
            "created": created,
            "hang": event_id == 1002,
        })
    events.sort(key=lambda e: e["time"] or datetime.min, reverse=True)
    return events


def classify_events(events: list, markers: list, starts: list) -> tuple:
    """(tray-tools のもの, 他のアプリのもの) に分ける。

    実際に落ちるのは venv のスタブが起こした本物のインタプリタなので、AppPath は venv では
    なく Python 本体を指す。障害のあるモジュールも Python 本体の _ctypes.pyd などのことが
    多い。だからパスだけでは決めきれない。パスに tray-tools の venv かリポジトリが出るか、
    プロセスの作成時刻の直後に crash.log の起動見出しがあれば tray-tools とする。
    それ以外(lan-drop など)は「他のアプリ」で、件数だけ出す。"""
    lowered = [m.lower() for m in markers if m]
    tray, other = [], []
    for event in events:
        joined = " ".join(event.get("paths") or []).lower()
        hit = any(m in joined for m in lowered)
        created = event.get("created")
        if not hit and created is not None:
            # 見出しは秒までしか書かないので、作成時刻(ミリ秒まである)より最大1秒早く見える。
            hit = any(-2 <= (start - created).total_seconds() <= START_MATCH_SECONDS
                      for start in starts)
        (tray if hit else other).append(event)
    return tray, other


def _decode_output(data: bytes) -> str:
    """wevtutil はコンソールのコードページ(日本語版なら cp932)で書く。UTF-8 から順に試す。"""
    for encoding in ("utf-8", "oem", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _looks_denied(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in (
        "access is denied", "unauthorizedaccess", "アクセスが拒否", "error 5", "エラー 5",
        "is not recognized", "blocked by group policy", "グループ ポリシー",
    ))


def _run(cmd: list, timeout: float):
    kwargs = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        # 常駐は pythonw で動く。コンソールの子を起こすと黒い窓が一瞬出るので止める。
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kwargs)


_PS_SCRIPT = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    " $s=(Get-Date).AddDays(-{days}); $e=$null;"
    # -ErrorAction Stop にすると、メッセージを組み立てられないイベントが1件混ざっただけで
    # 列挙ごと止まる(このPCで実際に起きた)。黙って飛ばし、何も取れなかったときだけ理由を見る。
    " $ev=@(Get-WinEvent -FilterHashtable @{{LogName='Application';Id=1000,1002;StartTime=$s}}"
    " -MaxEvents {count} -ErrorAction SilentlyContinue -ErrorVariable e);"
    " foreach ($x in $ev) {{ $m=''; try {{ $m=[Security.SecurityElement]::Escape([string]$x.Message) }} catch {{ }};"
    " $x.ToXml().Replace('</Event>','<RenderingInfo><Message>'+$m+'</Message></RenderingInfo></Event>') }};"
    " if ($ev.Count -eq 0 -and $e) {{ $id=($e | ForEach-Object {{ $_.FullyQualifiedErrorId }}) -join ' ';"
    " if ($id -notmatch 'NoMatchingEventsFound') {{ [Console]::Error.WriteLine($id + ' ' + $e[0].Exception.Message); exit 5 }} }}"
)


def read_event_log(days: int = EVENT_DAYS, fetch: int = EVENT_FETCH, timeout: float = EVENT_TIMEOUT):
    """("ok", xml文字列) / ("denied", 理由) / ("error", 理由)。

    まず wevtutil、だめなら PowerShell の Get-WinEvent。.ps1 は使わず -Command に本文を
    渡す(業務PCは GPO で .ps1 が止められていることがある)。"""
    query = ("*[System[(EventID=1000 or EventID=1002) and "
             f"TimeCreated[timediff(@SystemTime) <= {int(days) * 86400000}]]]")
    reasons = []
    denied = False
    for fmt in ("/f:RenderedXml", "/f:xml"):
        try:
            result = _run(["wevtutil", "qe", "Application", f"/q:{query}", fmt,
                           f"/c:{int(fetch)}", "/rd:true"], timeout)
        except (OSError, subprocess.SubprocessError) as e:
            reasons.append(f"wevtutil {type(e).__name__}")
            break
        if result.returncode == 0:
            return "ok", _decode_output(result.stdout)
        err = _decode_output(result.stderr or b"")
        denied = denied or _looks_denied(err)
        reasons.append(f"wevtutil rc={result.returncode}")
    try:
        result = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                       _PS_SCRIPT.format(days=int(days), count=int(fetch))], timeout + 10)
        output = _decode_output(result.stdout or b"")
        # 途中の1件で失敗しても、そこまでに書けた分は使う(メッセージの組み立てに失敗する
        # イベントが混ざることがある)。
        if result.returncode == 0 or "<Event" in output:
            return "ok", output
        err = _decode_output(result.stderr or b"")
        denied = denied or _looks_denied(err)
        reasons.append(f"ps rc={result.returncode}")
    except (OSError, subprocess.SubprocessError) as e:
        reasons.append(f"ps {type(e).__name__}")
    return ("denied" if denied else "error"), " ".join(reasons)


def summarize_events(status: str, tray: list, other: list, count: int, days: int = EVENT_DAYS) -> list:
    if status == "denied":
        return [f"evt {days}d 読めず (denied)"]
    if status != "ok":
        return [f"evt {days}d 読めず"]
    lines = [f"evt {days}d tray {len(tray)} other {len(other)}"]
    for event in tray[:count]:
        when = event["time"].strftime("%m-%d %H:%M") if event.get("time") else "?"
        app = (event.get("app") or "?").replace(".exe", "")
        if event.get("hang"):
            lines.append(f" {when} {app} hang")
        else:
            lines.append(f" {when} {app} {event.get('module') or '?'} {event.get('code') or '?'}")
    return lines


# ===============================================================
# crash.log / error.log
# ===============================================================
def _short_kind(kind: str) -> str:
    kind = kind.strip()
    known = {"access violation": "AV", "stack overflow": "stack-overflow",
             "illegal instruction": "illegal-instr", "int divide by zero": "div0"}
    if kind in known:
        return known[kind]
    match = re.fullmatch(r"code (0x[0-9a-fA-F]+)", kind)
    if match:
        return match.group(1).lower()
    return kind[:40]


def _is_maybe_handled(kind: str) -> bool:
    match = re.fullmatch(r"code (0x[0-9a-fA-F]+)", kind.strip())
    return bool(match) and match.group(1).lower().startswith(HANDLED_CODE_PREFIXES)


def parse_crash_log(text: str, count: int = DEFAULT_COUNT) -> dict:
    """{"starts": [起動日時…], "fatals": [直近 count 件], "fatal_total"}。

    fatal は {"start", "kind", "maybe_handled", "thread", "frames": [(ファイル名, 行, 関数)]}。
    faulthandler は all_threads=True で全スレッドを書くので、落ちたスレッドは
    「Current thread」の塊。無ければ先頭の塊を使う(最初の塊は待っているだけの
    keyboard のスレッドであることが多く、それを上位フレームとして出すと誤読する)。"""
    starts = []
    fatals = []
    current_start = None
    fatal = None
    block = None

    def close_fatal():
        nonlocal fatal, block
        if fatal is None:
            return
        chosen = next((b for b in fatal["blocks"] if b["current"]), None)
        if chosen is None and fatal["blocks"]:
            chosen = fatal["blocks"][0]
        fatals.append({
            "start": fatal["start"],
            "kind": _short_kind(fatal["kind"]),
            "maybe_handled": _is_maybe_handled(fatal["kind"]),
            "thread": (chosen or {}).get("name"),
            "frames": (chosen or {}).get("frames", [])[:3],
            "trigger": _gc_trigger((chosen or {}).get("frames", [])),
        })
        fatal = None
        block = None

    for line in (text or "").splitlines():
        heading = START_HEADING_RE.match(line)
        if heading:
            close_fatal()
            try:
                current_start = datetime.strptime(heading.group(1), "%Y-%m-%d %H:%M:%S")
                starts.append(current_start)
            except ValueError:
                current_start = None
            continue
        stripped = line.strip()
        if stripped.startswith("Windows fatal exception:") or stripped.startswith("Fatal Python error:"):
            close_fatal()
            fatal = {"start": current_start, "kind": stripped.split(":", 1)[1], "blocks": []}
            continue
        if fatal is None:
            continue
        thread = THREAD_RE.match(stripped)
        if thread:
            block = {"current": thread.group(1) == "Current thread",
                     "name": thread.group(2), "frames": []}
            fatal["blocks"].append(block)
            continue
        if block is None:
            continue
        if stripped == "Garbage-collecting":
            block["frames"].append(("GC", "", ""))
            continue
        frame = FRAME_RE.match(line)
        if frame:
            name = re.split(r"[\\/]", frame.group(1))[-1]
            block["frames"].append((name, frame.group(2), frame.group(3)))
    close_fatal()
    return {"starts": starts, "fatals": fatals[-count:] if count else [], "fatal_total": len(fatals)}


def _group(items, key) -> list:
    """続けて同じものを1つにまとめ、(先頭のもの, 回数) にする。新しい順に渡すので、
    先頭＝いちばん新しいもの。同じ落ち方が5回続いたときに5行並べても読む手間が増えるだけ。"""
    groups = []
    for item in items:
        k = key(item)
        if groups and groups[-1][0] == k:
            groups[-1][2] += 1
        else:
            groups.append([k, item, 1])
    return [(item, times) for _, item, times in groups]


def _gc_trigger(frames: list) -> list:
    """「GC → comtypes の Release / __del__」で始まるスタックなら、その下の3フレーム
    (GC を呼び込んだ処理)を返す。そうでなければ空。"""
    if not frames or frames[0][0] != "GC":
        return []
    rest = frames[1:]
    while rest and rest[0][2] in ("Release", "__del__"):
        rest = rest[1:]
    if len(rest) == len(frames) - 1:
        return []  # GC の直下が Release / __del__ ではない(別の形)
    return rest[:3]


def _format_frame(frame) -> str:
    name, line, func = frame
    if name == "GC":
        return "GC"
    return f"{name}:{line} {func}"


def summarize_crash(info: dict) -> list:
    fatals = info.get("fatals") or []
    lines = [f"crash fatal {info.get('fatal_total', 0)} (last {len(fatals)})"]
    for fatal, times in _group(reversed(fatals), lambda f: (
            f["kind"], f.get("maybe_handled"), f.get("thread"), tuple(f.get("frames") or []),
            tuple(f.get("trigger") or []))):
        when = fatal["start"].strftime("%m-%d %H:%M") if fatal.get("start") else "?"
        mark = "?" if fatal.get("maybe_handled") else ""
        if fatal.get("trigger"):
            # GC → comtypes の Release で落ちた形は上位3フレームがいつも同じで、何の最中に
            # GC が走ったかが読めない。その下(GC を呼び込んだ側)の3フレームを出す。
            frames = "GC>Release @ " + " < ".join(_format_frame(f) for f in fatal["trigger"])
        else:
            frames = " < ".join(_format_frame(f) for f in fatal.get("frames") or []) or "-"
        thread = f" [{fatal['thread']}]" if fatal.get("thread") else ""
        repeat = f" x{times}" if times > 1 else ""
        lines.append(f" {when}{repeat} {fatal['kind']}{mark}{thread} {frames}")
    if any(f.get("maybe_handled") for f in fatals):
        lines.append(" ?=maybe handled, not always a crash")
    return lines


def parse_error_log(text: str, count: int = DEFAULT_COUNT) -> dict:
    """{"entries": [直近 count 件], "total"}。entry は {"time", "where", "type", "message"}。

    見出しの後ろの本文から、最後に出てくる「型: メッセージ」の行を例外とする
    (連鎖した例外なら最後のものが最終的に投げられたもの)。メッセージは1行目だけ。"""
    entries = []
    current = None

    def close():
        if current is None:
            return
        exc_type, message = "?", ""
        for line in current["body"]:
            if not line or line[0].isspace() or line.startswith("Traceback"):
                continue
            match = EXC_LINE_RE.match(line)
            if match and (match.group(2) is not None or re.search(r"(Error|Exception|Exit|Interrupt|Warning)$", match.group(1))):
                exc_type, message = match.group(1), (match.group(2) or "")
        entries.append({"time": current["time"], "where": current["where"],
                        "type": exc_type, "message": message})

    for line in (text or "").splitlines():
        heading = ERROR_HEADING_RE.match(line)
        if heading:
            close()
            try:
                when = datetime.strptime(heading.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                when = None
            current = {"time": when, "where": heading.group(2).split(" ", 1)[0][:24], "body": []}
            continue
        if current is not None:
            current["body"].append(line.rstrip("\r"))
    close()
    return {"entries": entries[-count:] if count else [], "total": len(entries)}


def summarize_errors(info: dict) -> list:
    entries = info.get("entries") or []
    lines = [f"err {info.get('total', 0)} (last {len(entries)})"]
    for entry, times in _group(reversed(entries), lambda e: (
            e.get("where"), e["type"], e.get("message"))):
        when = entry["time"].strftime("%m-%d %H:%M") if entry.get("time") else "?"
        message = entry.get("message") or ""
        if len(message) > 70:
            message = message[:67] + "..."
        repeat = f" x{times}" if times > 1 else ""
        text = f" {when}{repeat} {entry.get('where') or '?'} {entry['type']}"
        lines.append(f"{text}: {message}" if message else text)
    return lines


def summarize_starts(starts: list, now: datetime, process_start=None) -> str:
    recent = [s for s in starts if timedelta(0) <= now - s <= timedelta(days=7)]
    last = max(starts).strftime("%m-%d %H:%M") if starts else "-"
    text = f"boot 7d {len(recent)} last {last}"
    if process_start is not None:
        seconds = max(0, int((now - process_start).total_seconds()))
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        text += f" up {days}d{hours:02d}h{rest // 60:02d}m"
    return text


# ===============================================================
# 設定(オンオフと件数だけ)
# ===============================================================
def _onoff(value) -> str:
    if value is None:
        return "?"
    return "on" if value else "off"


def summarize_settings(app_settings: dict, runtime: dict = None) -> list:
    """settings.json から主要機能のオンオフと件数だけを出す。値そのものは出さない。

    runtime は常駐から渡す「いま動いているか」(ジグラーなど、設定ではなく状態のもの)。"""
    s = app_settings if isinstance(app_settings, dict) else {}

    def section(name):
        value = s.get(name)
        return value if isinstance(value, dict) else {}

    hotkeys = section("hotkeys")
    assigned = sum(1 for v in hotkeys.values() if isinstance(v, str) and v.strip())
    devices = section("audio").get("devices")
    bookmarks = section("launcher").get("bookmarks")
    parts = [
        f"hk {assigned}/{len(hotkeys)}",
        f"tbw {_onoff(section('taskbar_widget').get('enabled'))}",
        f"prewarm {_onoff(section('capture').get('prewarm', True))}",
        f"audio {len(devices) if isinstance(devices, list) else 0}",
        f"bm {len(bookmarks) if isinstance(bookmarks, list) else 0}",
        f"pushcfg {_onoff(section('tools').get('pushover', False))}",
    ]
    profiles = s.get("copilot_profiles")
    if isinstance(profiles, list) and profiles:
        parts.append(f"cprof {len(profiles)}")
    lines = ["cfg " + " ".join(parts)]
    if runtime:
        labels = [("jig", "jiggler"), ("awake", "keep_awake"), ("cop", "copilot_bar"),
                  ("loop", "agent_loop"), ("mirror", "mirror")]
        run = [f"{short} {_onoff(runtime.get(key))}" for short, key in labels if key in runtime]
        if "sticky" in runtime:
            run.append(f"sticky {runtime['sticky']}")
        if run:
            lines.append("run " + " ".join(run))
    return lines


# ===============================================================
# 伏せ字(まとめの全文に最後にかける)
# ===============================================================
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_IPV4_RE = re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.])")
_IPV6_RE = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")
# 引用符で囲まれたパス(空白を含みうる)と、囲まれていないパス(空白で終わる)。
_QUOTED_PATH_RE = re.compile(r"""(['"])((?:[A-Za-z]:|\\\\)[\\/][^'"\r\n]*)\1""")
_BARE_PATH_RE = re.compile(r"""(?<![\w])(?:[A-Za-z]:[\\/]|\\\\[^\s\\/]+[\\/])[^\s'"<>|,;()]*""")


def _ipv6_like(text: str) -> bool:
    # 時刻(21:39:51)や「a:b:c」を IP と取り違えないよう、:: か 7個の : を要件にする。
    return "::" in text or text.count(":") >= 7


def _basename(path: str) -> str:
    path = path.rstrip("\\/")
    name = re.split(r"[\\/]", path)[-1]
    return name or "<path>"


def default_secret_names() -> list:
    """伏せる名前。環境変数のユーザー名・コンピュータ名・ドメイン名と、ホームのフォルダ名。
    COMPUTERNAME は NetBIOS 名(15文字で切れる)なので、ホスト名も足しておく。"""
    names = [os.environ.get(k, "") for k in ("USERNAME", "COMPUTERNAME", "USERDOMAIN", "USERDNSDOMAIN")]
    try:
        names.append(Path.home().name)
    except (RuntimeError, OSError):
        pass
    for getter in (socket.gethostname, platform.node):
        try:
            names.append(getter())
        except OSError:
            pass
    return names


def redact(text: str, home: str = None, names=None) -> str:
    """まとめの全文を伏せる。順番に意味がある。

    1. メールアドレスと URL(ユーザー名を含みうるので、名前の置換より先に丸ごと消す)
    2. ホームのパスを ~ に(~ の後ろはそのまま。中のユーザー名は 4 で伏せる)
    3. 他の絶対パスはファイル名だけに
    4. ユーザー名・コンピュータ名・ドメイン名を *(大文字小文字は区別しない)
    5. IP アドレス"""
    if home is None:
        try:
            home = str(Path.home())
        except (RuntimeError, OSError):
            home = ""
    if names is None:
        names = default_secret_names()
    text = _EMAIL_RE.sub("<email>", text)
    text = _URL_RE.sub("<url>", text)

    if home and len(home) > 3:
        variants = {home, home.replace("\\", "/"), home.replace("/", "\\"),
                    home.replace("\\", "\\\\")}
        for variant in sorted(variants, key=len, reverse=True):
            text = re.sub(re.escape(variant) + r"(?![\w.-])", "~", text, flags=re.I)

    text = _QUOTED_PATH_RE.sub(lambda m: m.group(1) + _basename(m.group(2)) + m.group(1), text)
    text = _BARE_PATH_RE.sub(lambda m: _basename(m.group(0)), text)

    unique = {n.strip() for n in names if n and n.strip()}
    for name in sorted(unique, key=len, reverse=True):
        if len(name) >= 3:
            text = re.sub(re.escape(name), "*", text, flags=re.I)
        else:
            # 1〜2文字の名前を部分一致で消すと本文がずたずたになる。単語としてだけ消す。
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", "*", text, flags=re.I)

    text = _IPV4_RE.sub("<ip>", text)
    text = _IPV6_RE.sub(lambda m: "<ip>" if _ipv6_like(m.group(0)) else m.group(0), text)
    return text


# ===============================================================
# ページ分け
# ===============================================================
def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _hard_split(line: str, limit: int) -> list:
    """上限を超える1行だけは文字の切れ目で割る(普通は起きない。保険)。"""
    pieces, current = [], ""
    for ch in line:
        if current and _utf8_len(current + ch) > limit:
            pieces.append(current)
            current = ""
        current += ch
    if current:
        pieces.append(current)
    return pieces


def split_pages(text: str, limit: int = PAGE_LIMIT_BYTES) -> list:
    """QR 1枚ずつのページに分ける。収まれば1ページ(印なし)。

    分けるときは行の切れ目で切り、各ページの先頭に [tray-tools diag 1/3] の印を付ける。
    印を含めて各ページが limit バイト以下になる。"""
    text = text.rstrip("\n")
    if _utf8_len(text) <= limit:
        return [text]
    total_guess = 2
    while True:
        header_len = _utf8_len(PAGE_MARK.format(page=total_guess, total=total_guess)) + 1
        budget = max(16, limit - header_len)
        lines = []
        for line in text.split("\n"):
            lines.extend(_hard_split(line, budget) if _utf8_len(line) > budget else [line])
        pages, current, size = [], [], 0
        for line in lines:
            add = _utf8_len(line) + (1 if current else 0)
            if current and size + add > budget:
                pages.append(current)
                current, size = [], 0
                add = _utf8_len(line)
            current.append(line)
            size += add
        if current:
            pages.append(current)
        if len(str(len(pages))) <= len(str(total_guess)):
            total = len(pages)
            return [PAGE_MARK.format(page=i + 1, total=total) + "\n" + "\n".join(body)
                    for i, body in enumerate(pages)]
        total_guess = len(pages)


# ===============================================================
# QR(segno)
# ===============================================================
def make_qr(page: str):
    """1ページぶんの QR。誤り訂正 M、バイトモード(UTF-8)、Micro QR にはしない。"""
    import segno
    return segno.make_qr(page.encode("utf-8"), error=QR_ERROR, mode="byte", boost_error=False)


def qr_to_qimage(qr, scale: int = 1, border: int = QR_BORDER):
    """segno の QR を QImage(白地に黒、周りに quiet zone border セル)に描く。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter

    matrix = [list(row) for row in qr.matrix]
    cells = len(matrix)
    size = (cells + border * 2) * scale
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    try:
        black = QColor("#000000")
        for y, row in enumerate(matrix):
            for x, bit in enumerate(row):
                if bit:
                    painter.fillRect(QRect((x + border) * scale, (y + border) * scale, scale, scale), black)
    finally:
        painter.end()
    return image


# ===============================================================
# まとめを作る(実物を読む)
# ===============================================================
def _read_tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return ""
    if size > limit:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else data
    return data.decode("utf-8", errors="replace")


def process_start_time():
    """このプロセスの起動時刻(ローカル、naive)。取れなければ None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(kernel32.GetCurrentProcess(), *[ctypes.byref(t) for t in times]):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return _filetime_to_local(created)
    except Exception:  # noqa: BLE001
        return None


def load_settings_readonly(path=None) -> dict:
    """settings.json を読むだけ(既定値と重ねる)。settings.load_settings は無ければファイルを
    作るので使わない。読めなければ空の辞書。"""
    path = Path(path) if path else SETTINGS_PATH
    try:
        import settings as settings_module
        merged = json.loads(json.dumps(settings_module.DEFAULT_SETTINGS))
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                merged = settings_module._deep_merge(merged, loaded)
        return merged
    except Exception:  # noqa: BLE001
        return {}


def _section(lines: list, label: str, fn):
    """1項目ぶん。失敗しても他の項目は出す。"""
    try:
        result = fn()
        lines.extend(result if isinstance(result, list) else [result])
    except Exception as e:  # noqa: BLE001
        lines.append(f"{label} 取得失敗 {type(e).__name__}")


def collect(app_settings: dict = None, runtime: dict = None, count: int = DEFAULT_COUNT,
            now: datetime = None, read_events=True, process_start=None) -> str:
    """実物を読んでまとめを作り、伏せ字をかけて返す。数秒かかることがある(イベントログ)。

    窓のプロセス(diagnostics_window)のワーカースレッドから呼ぶ。Qt には触らない。
    process_start は「今回の起動」の時刻。窓は別プロセスなので、常駐の起動時刻を
    引数で受け取る(無ければこのプロセスの起動時刻)。"""
    now = now or datetime.now()
    if app_settings is None:
        app_settings = load_settings_readonly()
    if process_start is None:
        process_start = process_start_time()
    lines = [f"[tray-tools diag] {now:%Y-%m-%d %H:%M}"]
    _section(lines, "ver", lambda: summarize_version(read_git_version(REPO_DIR)))
    _section(lines, "py", lambda: summarize_files(scan_files(REPO_DIR)))
    _section(lines, "os", lambda: summarize_environment(
        _windows_build(), platform.python_version(), package_versions()))

    crash = {"starts": [], "fatals": [], "fatal_total": 0}
    try:
        crash = parse_crash_log(_read_tail(CRASH_LOG_PATH), count)
    except Exception as e:  # noqa: BLE001
        lines.append(f"crash 取得失敗 {type(e).__name__}")
    _section(lines, "boot", lambda: summarize_starts(crash["starts"], now, process_start))
    _section(lines, "cfg", lambda: summarize_settings(app_settings, runtime))

    def events():
        if not read_events:
            return ["evt skipped"]
        status, payload = read_event_log()
        if status != "ok":
            return summarize_events(status, [], [], count)
        markers = [str(REPO_DIR), sys.prefix, "tray-tools"]
        tray, other = classify_events(parse_event_xml(payload), markers, crash["starts"])
        return summarize_events(status, tray, other, count)

    _section(lines, "evt", events)
    _section(lines, "crash", lambda: summarize_crash(crash))
    _section(lines, "err", lambda: summarize_errors(parse_error_log(_read_tail(ERROR_LOG_PATH), count)))
    return redact("\n".join(lines))


WINDOW_SCRIPT = REPO_DIR / "diagnostics_window.py"


def spawn_window(runtime: dict = None, settings_path=None) -> int:
    """診断の窓を別プロセスで起こす(常駐から呼ぶ)。起こせなければ OSError。

    なぜ別プロセスか: 最初は常駐の中に窓を出し、まとめはワーカースレッドで作っていた。
    実機で開いた直後に常駐ごと落ちた(crash.log: QR を作っている最中の GC が comtypes の
    __del__ → Release に入って access violation。イベントログにも _ctypes.pyd の c0000005)。
    常駐には pycaw などの COM オブジェクトが居て、どのスレッドで GC が走っても事故の
    種になる。QR の生成は一度に大量のオブジェクトを作るので GC を呼び込みやすい。
    背景を透過(bg_remove_launch)や Copilot の状態監視と同じく、重い処理は常駐の外で行う。

    常駐でしか分からないもの(いま動いている機能・常駐の起動時刻)だけを引数で渡す。"""
    from bg_remove_launch import _allow_foreground
    from traytools_send import CREATE_NEW_PROCESS_GROUP, DETACHED_PROCESS, pythonw_executable

    if not WINDOW_SCRIPT.is_file():
        raise OSError(f"{WINDOW_SCRIPT.name} がありません")
    argv = [pythonw_executable(), str(WINDOW_SCRIPT),
            "--runtime", json.dumps(runtime or {}, ensure_ascii=True)]
    start = process_start_time()
    if start is not None:
        argv += ["--parent-start", start.strftime("%Y-%m-%dT%H:%M:%S")]
    if settings_path:
        argv += ["--settings", str(settings_path)]
    _allow_foreground()
    proc = subprocess.Popen(argv, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                            close_fds=True)
    return proc.pid


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    text = collect(read_events="--no-events" not in argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if "--pages" in argv:
        pages = split_pages(text)
        for page in pages:
            print(page)
            print(f"--- {_utf8_len(page)} bytes")
    else:
        print(text)
        print(f"--- {_utf8_len(text)} bytes, {len(split_pages(text))} page(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
