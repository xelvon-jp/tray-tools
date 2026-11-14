# script_runner_core.py
# 「スクリプト実行パネル」(script_runner.py)の、窓と切り離した純粋部分。
# チェック(読むだけ)・実行・クリップボードに入れる文の組み立て・実行の記録を
# ここに置く。Qt には依存しない(tests/test_script_runner.py がそのまま叩く)。
#
# 仕様の正本は collab-workspace の docs/work-tools/script-runner-spec.md
# (アオイの追記を含む版)。要点:
#   - チェックは読むだけ。対象ファイルを実行することは絶対にしない。
#     Python の文法と import は「実行に使う Python」のサブプロセスで確かめる
#     (パネル自身の Python と版が違うと判定がずれるため)。import の確認は
#     最上位の名前だけ(find_spec("a.b") は親を読み込んでしまうため)。
#   - 実行は呼ぶ側(窓)が F5 かボタンを押したときだけ。ここは実行の部品だけ。
#   - 実行の直前に sha256 を取り直すのは窓側の仕事。execute_script に
#     expected_sha256 を渡せば、起動の直前にもここで取り直して比べる
#     (低8)。記録の sha256 は実行したときのもの。
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# チェック結果の3値(+チェック不能)。
STATUS_OK = "ok"            # 問題なし
STATUS_ERROR = "error"      # 文法エラー・読めない import・文字コード(実行不可)
STATUS_WARN = "warn"        # 要確認(F5 では実行しない)
STATUS_UNKNOWN = "unknown"  # チェック不能(Python / PowerShell が見つからない)

LOG_FILENAME = "script_runner_log.jsonl"
PS_ALLOWLIST_FILENAME = "script_runner_ps_allowlist.json"

# ブラウザの書きかけなど、対象にしない末尾。
PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp")
TARGET_EXTENSIONS = (".py", ".ps1")

# BOM 無し UTF-8 の .ps1 を PowerShell 5.1 に読ませるための写しの
# 接頭辞(高1)。写しは元の隣に作って実行後に消す。一覧からは外す。
BOM_COPY_PREFIX = ".sr-bom-"
UTF8_BOM = b"\xef\xbb\xbf"

CHECK_TIMEOUT_SECONDS = 20
PS_CHECK_TIMEOUT_SECONDS = 30

# 実行ポリシーで止まったときに結果の文へ足す注意(仕様のアオイ追記6)。
POLICY_NOTE = "実行ポリシーで止まりました。この段では .ps1 はチェックだけになります"
_POLICY_MARKERS = (
    "PSSecurityException",
    "running scripts is disabled",
    "execution policy",
    "実行ポリシー",
)


def data_dir() -> Path:
    """アプリデータの置き場(%LOCALAPPDATA% の下の traytools フォルダ)。
    tomorrow_memo.data_dir と同じ作法(TRAYTOOLS_DATA_DIR で差し替え可)。"""
    override = os.environ.get("TRAYTOOLS_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "traytools"


def default_watch_folder() -> Path:
    """既定の見張りフォルダ(ダウンロードフォルダ)。

    Windows では User Shell Folders のダウンロード(GUID
    {374DE290-123F-4565-9164-39C4925E467B})をレジストリから引く
    (OneDrive へ向け替えられている環境でも実際の場所になる)。
    引けなければ ~/Downloads。"""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer"
            r"\User Shell Folders")
        try:
            value, _kind = winreg.QueryValueEx(
                key, "{374DE290-123F-4565-9164-39C4925E467B}")
        finally:
            winreg.CloseKey(key)
        expanded = os.path.expandvars(str(value)).strip()
        if expanded:
            return Path(expanded)
    except Exception:  # noqa: BLE001  引けない環境では既定の場所へ
        pass
    return Path.home() / "Downloads"


# ---------------------------------------------------------------
# 設定(script_runner 節)
# ---------------------------------------------------------------
DEFAULT_CONFIG = {
    "watch_folder": "",        # 空ならダウンロードフォルダ
    "recent_hours": 24,        # 開いた時点で並べる「直近」の幅
    "max_items": 20,           # 一覧の上限
    "python": "python",        # 実行に使う Python(パスか PATH 上の名前。"py" も可)
    "timeout_seconds": 120,    # 実行の時間切れ
    "head_lines": 200,         # クリップボード文の切り詰め(先頭)
    "tail_lines": 50,          # クリップボード文の切り詰め(末尾)
    "max_chars": 20000,        # クリップボード文の切り詰め(全体の文字数)
    "window_geometry": None,   # [x, y, w, h]
    "always_on_top": False,    # 前面に固定
}


def _int_or(value, default, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def normalize_config(section) -> dict:
    """settings.json の script_runner 節を、欠けた項目を既定で埋めた辞書にする。
    壊れた値は既定へ落とす(手で編集するファイルなので、1つの間違いで
    パネル全体が開かなくなるのを避ける)。"""
    config = dict(DEFAULT_CONFIG)
    if isinstance(section, dict):
        for key in DEFAULT_CONFIG:
            if key in section:
                config[key] = section[key]
    if not isinstance(config["watch_folder"], str):
        config["watch_folder"] = ""
    config["recent_hours"] = _int_or(config["recent_hours"], 24, 1, 24 * 30)
    config["max_items"] = _int_or(config["max_items"], 20, 1, 100)
    if not isinstance(config["python"], str) or not config["python"].strip():
        config["python"] = "python"
    config["timeout_seconds"] = _int_or(config["timeout_seconds"], 120, 1, 3600)
    config["head_lines"] = _int_or(config["head_lines"], 200, 0, 100000)
    config["tail_lines"] = _int_or(config["tail_lines"], 50, 0, 100000)
    config["max_chars"] = _int_or(config["max_chars"], 20000, 100, 1000000)
    geometry = config["window_geometry"]
    if not (isinstance(geometry, list) and len(geometry) == 4
            and all(isinstance(v, int) for v in geometry)):
        config["window_geometry"] = None
    config["always_on_top"] = bool(config["always_on_top"])
    return config


def resolve_watch_folder(config: dict) -> Path:
    """見張るフォルダ。設定が空ならダウンロードフォルダ。"""
    folder = str(config.get("watch_folder") or "").strip()
    if folder:
        return Path(os.path.expandvars(folder)).expanduser()
    return default_watch_folder()


def file_sha256(path) -> str:
    """ファイルの sha256(16進)。読めなければ ""。"""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def list_candidates(folder, cutoff_ts: float, limit: int) -> list:
    """見張りフォルダ直下の .py / .ps1 を新しい順に並べる。

    戻り値は [(Path, mtime, size), ...]。書きかけ(.crdownload 等)は
    拡張子の時点で外れるが、"x.py.part" のような名前も末尾で弾く。
    cutoff_ts より古いものは含めない。"""
    folder = Path(folder)
    found = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return found
    for entry in entries:
        name = entry.name
        lowered = name.lower()
        if lowered.endswith(PARTIAL_SUFFIXES):
            continue
        if name.startswith(BOM_COPY_PREFIX):
            continue  # BOM 付け写し(実行中だけ隣に居る)は対象にしない
        if not lowered.endswith(TARGET_EXTENSIONS):
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff_ts:
            continue
        found.append((entry, stat.st_mtime, stat.st_size))
    # 新しい順。同点はパスで並べて、順序が安定するようにする。
    found.sort(key=lambda item: (-item[1], str(item[0])))
    return found[:limit]


# ---------------------------------------------------------------
# チェック結果
# ---------------------------------------------------------------
@dataclass
class CheckIssue:
    line: int = 0               # 1始まり。行が無い話は 0
    message: str = ""           # 1行の説明
    text: str = ""              # 該当行の本文(あれば)
    offset: int = 0             # 桁(1始まり。あれば)


@dataclass
class CheckResult:
    status: str = STATUS_OK
    kind: str = ""              # syntax / import / encoding / danger / policy …
    issues: list = field(default_factory=list)   # [CheckIssue]
    headline: str = ""          # 一覧の行に出す1行
    jump_line: int = 0          # EDDy で開くときに飛ぶ行
    sha256: str = ""            # チェックした時点の中身


# ---------------------------------------------------------------
# Python のチェック
# ---------------------------------------------------------------
# 要確認の名前の一覧(データ)。足すときはここへ1行足すだけでよい。
#   paths  … ドットつなぎの呼び出し名(os.remove など)
#   attrs  … 受け手が何でも、メソッド名だけで拾うもの(Path.unlink など)
#   names  … 裸の名前の呼び出し(exec など)と、getattr のような組み立て呼び出し
#   modules… 読み込むだけで要確認にするモジュール(最上位の名前でも、
#             http.client のような下位まででも拾う)
PYTHON_DANGER = {
    "paths": {
        "os.remove": "ファイルを消す呼び出し",
        "os.unlink": "ファイルを消す呼び出し",
        "os.rmdir": "フォルダを消す呼び出し",
        "os.rename": "名前を変える呼び出し",
        "os.replace": "名前を変える呼び出し",
        "os.system": "外のコマンドを呼ぶ呼び出し",
        "os.popen": "外のコマンドを呼ぶ呼び出し",
        "os.startfile": "外のファイル・アプリを開く呼び出し",
        "os.makedirs": "フォルダを作る呼び出し",
        "shutil.rmtree": "フォルダを丸ごと消す呼び出し",
        "shutil.move": "ファイルを動かす呼び出し",
        "shutil.copy": "ファイルをコピーする呼び出し(上書き先)",
        "shutil.copy2": "ファイルをコピーする呼び出し(上書き先)",
        "shutil.copyfile": "ファイルをコピーする呼び出し(上書き先)",
        "shutil.copytree": "フォルダをコピーする呼び出し(上書き先)",
    },
    "attrs": {
        "unlink": "ファイルを消す呼び出し",
        "rmtree": "フォルダを丸ごと消す呼び出し",
        "write_text": "ファイルへ書き込む呼び出し",
        "write_bytes": "ファイルへ書き込む呼び出し",
        "rename": "名前を変える呼び出し",
        "replace": "名前を変える呼び出し",
        "rmdir": "フォルダを消す呼び出し",
        "touch": "ファイルを作る・更新する呼び出し",
        "mkdir": "フォルダを作る呼び出し",
    },
    "names": {
        "exec": "動的に実行する呼び出し",
        "eval": "動的に実行する呼び出し",
        "compile": "動的に実行する呼び出し",
        "__import__": "動的に読み込む呼び出し",
        "getattr": "名前を組み立てて呼ぶ呼び出し",
    },
    "modules": [
        "subprocess", "urllib", "requests", "socket", "importlib",
        "ctypes", "winreg", "ftplib", "smtplib", "http.client",
    ],
    "module_label": "外へつなぐ・動かすモジュールの読み込み",
}

# 実行に使う Python の中で走らせる検査役。入力は JSON
# ({"source": 本文, "danger": PYTHON_DANGER})、出力は JSON 1行。
# 対象ファイルを実行することは無い(ast で読むだけ。import の確認は
# find_spec で、最上位の名前だけを調べる)。
_PY_CHECKER_SRC = r"""
import ast
import json
import sys
from importlib.machinery import PathFinder


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def main():
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    source = payload["source"]
    danger = payload["danger"]
    out = {"syntax": None, "missing": [], "dangers": []}
    try:
        tree = compile(source, "<check>", "exec", ast.PyCF_ONLY_AST)
    except SyntaxError as e:
        out["syntax"] = {
            "line": e.lineno or 0, "offset": e.offset or 0,
            "msg": e.msg or str(e),
            "text": (e.text or "").rstrip("\n").rstrip("\r"),
        }
        print(json.dumps(out))
        return
    lines = source.split("\n")

    def srcline(n):
        return lines[n - 1] if 1 <= n <= len(lines) else ""

    # import の確認: 本体の直下の文だけ(try の中は対象外 = 本体の直下には
    # 現れない)。import a.b / from a.b import c は最上位の a だけを調べる。
    tops = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                tops.append((alias.name.split(".")[0], stmt.lineno))
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.level:
                continue
            if stmt.module:
                tops.append((stmt.module.split(".")[0], stmt.lineno))
    seen = set()
    # スクリプトと同じフォルダの自作モジュールも「ある」扱いにする
    # ため、探す場所の先頭にスクリプトのフォルダを足す(中4)。
    search_path = [p for p in [payload.get("script_dir")] if p]
    search_path += sys.path
    for name, line in tops:
        if name in seen:
            continue
        seen.add(name)
        try:
            spec = PathFinder.find_spec(name, search_path)
        except Exception:
            spec = None
        if spec is None:
            out["missing"].append({"name": name, "line": line})

    danger_paths = danger["paths"]
    danger_attrs = danger["attrs"]
    danger_names = danger["names"]
    danger_modules = set(danger["modules"])
    module_label = danger["module_label"]
    found = []

    def add(line, name, label):
        found.append({"line": line, "name": name, "label": label,
                      "text": srcline(line)})

    # import の別名と from 取り込みを、先に名前対応表へ直す(中3)。
    #   import os as o        → o は os
    #   from os import remove → remove は os.remove
    # これで o.remove(...) や remove(...) も一覧と照らせる。
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    bindings[top] = top
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            for alias in node.names:
                bindings[alias.asname or alias.name] = \
                    node.module + "." + alias.name

    def resolve(path):
        if not path:
            return path
        head, dot, rest = path.partition(".")
        if head in bindings:
            return bindings[head] + (dot + rest if dot else "")
        return path

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif node.module:
                mods = [node.module]
            for mod in mods:
                if mod in danger_modules or mod.split(".")[0] in danger_modules:
                    add(node.lineno, mod, module_label)
        elif isinstance(node, ast.Call):
            func = node.func
            called = None
            path = None
            if isinstance(func, ast.Name):
                called = func.id
                path = func.id
            elif isinstance(func, ast.Attribute):
                called = func.attr
                path = _dotted(func)
            if path and resolve(path) in danger_paths:
                add(node.lineno, resolve(path),
                    danger_paths[resolve(path)])
            elif isinstance(func, ast.Attribute) and func.attr in danger_attrs:
                add(node.lineno, func.attr, danger_attrs[func.attr])
            if called in danger_names:
                add(node.lineno, called, danger_names[called])
            if called == "open" and isinstance(
                    func, (ast.Name, ast.Attribute)):
                # 組み込みの open(ファイル, モード) と、Path.open
                # (モード, …) ではモード引数の位置が違う。
                builtin_style = isinstance(func, ast.Name) \
                    or resolve(path) == "io.open"
                mode = None
                unreadable = False
                mode_arg = None
                if builtin_style and len(node.args) >= 2:
                    mode_arg = node.args[1]
                elif not builtin_style and len(node.args) >= 1:
                    mode_arg = node.args[0]
                if mode_arg is not None:
                    if isinstance(mode_arg, ast.Constant) \
                            and isinstance(mode_arg.value, str):
                        mode = mode_arg.value
                    else:
                        unreadable = True
                else:
                    for keyword in node.keywords:
                        if keyword.arg == "mode":
                            value = keyword.value
                            if isinstance(value, ast.Constant) \
                                    and isinstance(value.value, str):
                                mode = value.value
                            else:
                                unreadable = True
                if unreadable:
                    add(node.lineno, "open",
                        "ファイルへ書き込む可能性のある open(モードが読めません)")
                elif mode and any(c in mode for c in "wax+"):
                    add(node.lineno, "open", "ファイルへ書き込む open")
    found.sort(key=lambda d: d["line"])
    out["dangers"] = found
    print(json.dumps(out))


main()
"""


def _run_checker(python_exe: str, source: str, script_dir: str = ""):
    """検査役をサブプロセスで走らせ、結果の dict を返す。失敗したら None。

    検査役の作業フォルダは空の一時フォルダにする(中4)。スクリプトの
    フォルダで走らせると、隣のモジュールがチェック中に読み込まれて
    実行されうるため。同フォルダのモジュールの有無は、探す場所に
    スクリプトのフォルダを足すことで別に確かめる。"""
    payload = json.dumps({"source": source, "danger": PYTHON_DANGER,
                          "script_dir": script_dir})
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        with tempfile.TemporaryDirectory() as empty_cwd:
            proc = subprocess.run(
                [python_exe, "-c", _PY_CHECKER_SRC],
                input=payload.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=CHECK_TIMEOUT_SECONDS, creationflags=flags,
                cwd=empty_cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def check_python_script(path, python_exe: str) -> CheckResult:
    """Python スクリプトのチェック(読むだけ)。窓と切り離した純関数。

    python_exe は「実行に使う Python」。文法と import はその Python の
    サブプロセスで確かめる。"""
    path = Path(path)
    result = CheckResult(sha256=file_sha256(path))
    try:
        raw = path.read_bytes()
    except OSError:
        result.status = STATUS_UNKNOWN
        result.kind = "gone"
        result.headline = "ファイルが読めません"
        return result
    result.sha256 = hashlib.sha256(raw).hexdigest()
    has_bom = raw.startswith(UTF8_BOM)
    # 読み方は coding 宣言に従う(低9)。宣言が無ければ UTF-8
    # (BOM があれば utf-8-sig として判定される)。
    try:
        detected, _consumed = tokenize.detect_encoding(
            io.BytesIO(raw).readline)
    except (SyntaxError, UnicodeDecodeError, LookupError):
        detected = None
    try:
        source = raw.decode(detected or "utf-8")
    except (UnicodeDecodeError, LookupError):
        result.status = STATUS_ERROR
        result.kind = "encoding"
        result.issues = [CheckIssue(
            message="UTF-8 として読めません"
                    "(cp932 などで保存されている可能性があります)")]
        result.headline = "文字コードが UTF-8 ではありません"
        return result
    checked = _run_checker(python_exe, source, str(path.parent))
    if checked is None:
        result.status = STATUS_UNKNOWN
        result.kind = "no_python"
        result.headline = "実行に使う Python でチェックできませんでした"
        return result
    syntax = checked.get("syntax")
    if syntax:
        result.status = STATUS_ERROR
        result.kind = "syntax"
        result.issues = [CheckIssue(
            line=int(syntax.get("line") or 0),
            message=str(syntax.get("msg") or ""),
            text=str(syntax.get("text") or ""),
            offset=int(syntax.get("offset") or 0))]
        result.jump_line = result.issues[0].line
        result.headline = f"文法エラー {len(result.issues)}件"
        return result
    source_lines = source.split("\n")

    def line_text(n):
        return source_lines[n - 1] if 1 <= n <= len(source_lines) else ""

    missing = checked.get("missing") or []
    if missing:
        result.status = STATUS_ERROR
        result.kind = "import"
        result.issues = [
            CheckIssue(
                line=int(item.get("line") or 0),
                message=f"モジュール '{item.get('name')}' が見つかりません"
                        "(実行に使う Python には入っていません)",
                text=line_text(int(item.get("line") or 0)))
            for item in missing]
        result.jump_line = result.issues[0].line
        result.headline = f"読み込めないモジュール {len(missing)}件"
        return result
    dangers = checked.get("dangers") or []
    if dangers:
        result.status = STATUS_WARN
        result.kind = "danger"
        result.issues = [
            CheckIssue(
                line=int(item.get("line") or 0),
                message=f"{item.get('label')}({item.get('name')})",
                text=str(item.get("text") or ""))
            for item in dangers]
        first = dangers[0]
        result.jump_line = int(first.get("line") or 0)
        result.headline = f"要確認・{first.get('label')}があります"
        return result
    if has_bom:
        result.status = STATUS_WARN
        result.kind = "bom"
        result.issues = [CheckIssue(message="BOM 付きのファイルです")]
        result.headline = "要確認・BOM 付きです"
        return result
    result.status = STATUS_OK
    result.headline = "問題なし"
    return result


# ---------------------------------------------------------------
# PowerShell のチェック
# ---------------------------------------------------------------
def load_ps_allowlist(path=None) -> list:
    """許可リスト(JSON データファイル)。読めなければ組み込みの既定。

    足すときは陽太さんの OK を取る決まり(仕様)。既定はこのファイルと
    同じ内容で、データファイルが無い環境でもチェックが動くための保険。"""
    if path is None:
        path = Path(__file__).resolve().parent / PS_ALLOWLIST_FILENAME
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        allowed = data.get("allowed")
        if isinstance(allowed, list) and allowed:
            return [str(item) for item in allowed]
    except (OSError, ValueError, AttributeError):
        pass
    return list(_PS_ALLOWLIST_DEFAULT)


_PS_ALLOWLIST_DEFAULT = [
    "Get-*", "Select-*", "Measure-*", "Test-Path", "Sort-Object",
    "Group-Object", "Where-Object", "ForEach-Object", "Format-*",
    "Out-String", "Write-Output", "ConvertTo-*",
    # パネルがあとで上書きするので、黄にしない(仕様のアオイ追記4)。
    "Set-Clipboard",
]


def find_powershell() -> str:
    """powershell.exe のパス。無ければ ""。"""
    return shutil.which("powershell.exe") or shutil.which("powershell") or ""


_PS_CHECKER_SRC = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$path = $env:SCRIPT_RUNNER_TARGET
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $path, [ref]$tokens, [ref]$errors)
$errList = @()
foreach ($e in $errors) {
    $errList += @{ line = $e.Extent.StartLineNumber; message = $e.Message
                   text = $e.Extent.Text }
}
$cmds = @()
$cmdAsts = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.CommandAst] },
    $true)
foreach ($c in $cmdAsts) {
    $name = $null
    try { $name = $c.GetCommandName() } catch { }
    $cmds += @{ name = $name; line = $c.Extent.StartLineNumber
                op = [string]$c.InvocationOperator; text = $c.Extent.Text }
}
$redirs = @()
foreach ($r in $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst] },
    $true)) {
    $redirs += @{ line = $r.Extent.StartLineNumber; append = [bool]$r.Append }
}
$statics = @()
$dynamics = @()
$methods = @()
foreach ($m in $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] },
    $true)) {
    $methods += @{ line = $m.Extent.StartLineNumber
                   member = [string]$m.Member.Value }
    if ($m.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) {
        $statics += @{ line = $m.Extent.StartLineNumber
                       type = $m.Expression.TypeName.FullName
                       member = [string]$m.Member.Value }
    } elseif ([string]$m.Member.Value -eq 'Invoke') {
        $dynamics += @{ line = $m.Extent.StartLineNumber; what = 'Invoke()' }
    }
}
$resolved = @{}
foreach ($c in $cmds) {
    $n = $c.name
    if ($n -and ($n -notmatch '-') -and -not $resolved.ContainsKey($n)) {
        $cur = $n
        for ($i = 0; $i -lt 5; $i++) {
            $alias = Get-Alias -Name $cur -ErrorAction SilentlyContinue
            if (-not $alias) { break }
            $cur = [string]$alias.Definition
            if ($cur -match ' ') { $cur = ($cur -split ' ')[0] }
        }
        $resolved[$n] = $cur
    }
}
@{ errors = $errList; commands = $cmds; redirections = $redirs
   statics = $statics; dynamics = $dynamics; methods = $methods
   resolved = $resolved } |
    ConvertTo-Json -Compress -Depth 6
"""


def _ps_allowed(name: str, allowlist) -> bool:
    import fnmatch

    lowered = name.lower()
    for pattern in allowlist:
        if fnmatch.fnmatchcase(lowered, str(pattern).lower()):
            return True
    return False


# 要確認にするメソッド名(中2)。型名付きの静的呼び出しだけでなく、
# (Get-Item x).Delete() のような受け手つきの呼び出しも拾うため、
# InvokeMemberExpressionAst の全部と照らす。
_PS_MEMBER_PATTERNS = ("delete", "move*", "copy*", "create*", "kill",
                       "start", "invoke*", "write*", "set*")


def _ps_member_warns(methods) -> list:
    """メソッド呼び出しの一覧 [{line, member}] から、要確認の
    (行, 文) を作る。窓の無い環境でも確かめられるよう分けてある。"""
    import fnmatch

    warns = []
    for item in methods or []:
        member = str(item.get("member") or "")
        if not member:
            continue
        lowered = member.lower()
        if any(fnmatch.fnmatchcase(lowered, pattern)
               for pattern in _PS_MEMBER_PATTERNS):
            warns.append((int(item.get("line") or 0),
                          f"メソッド呼び出しがあります({member})"))
    return warns


def ps1_bom_copy_source(path):
    """BOM を付けた写しが要る .ps1 なら元のバイト列、要らなければ
    None(高1)。

    PowerShell 5.1 は、BOM の無いファイルを cp932 として読む。
    BOM 無しで ASCII 以外のバイトを含む UTF-8 の .ps1 は、そのまま
    ではチェック(ParseFile)も実行(-File)も文法エラーになるため、
    BOM 付きの写しで読ませる。BOM 付き・ASCII のみは従来どおり
    元のファイルをそのまま使う。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if raw.startswith(UTF8_BOM):
        return None
    if all(byte < 0x80 for byte in raw):
        return None
    return raw


def check_powershell_script(path, powershell_exe=None, allowlist=None) -> CheckResult:
    """PowerShell スクリプトのチェック(読むだけ)。窓と切り離した純関数。

    文法と命令名は PowerShell 5.1 のパーサー(ParseFile)で取る。
    powershell.exe が無い環境では STATUS_UNKNOWN(チェック不能)。"""
    path = Path(path)
    result = CheckResult(sha256=file_sha256(path))
    exe = powershell_exe or find_powershell()
    if not exe:
        result.status = STATUS_UNKNOWN
        result.kind = "no_powershell"
        result.headline = "PowerShell が無いためチェックできません"
        return result
    if allowlist is None:
        allowlist = load_ps_allowlist()
    # 高1: BOM 無し UTF-8 の .ps1 は、そのままでは PowerShell 5.1 が
    # cp932 として読んで文法エラーになる。BOM を付けた写しを一時
    # フォルダに作ってチェックし、エラー表示に出る写しのパスは
    # 元のファイル名へ置き換える。sha256 は常に元のファイルのもの。
    bom_raw = ps1_bom_copy_source(path)
    if bom_raw is None:
        return _run_ps_checker(path, exe, allowlist, result)
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / path.name
        try:
            copy_path.write_bytes(UTF8_BOM + bom_raw)
        except OSError:
            return _run_ps_checker(path, exe, allowlist, result)
        checked_result = _run_ps_checker(copy_path, exe, allowlist, result)
        for issue in checked_result.issues:
            issue.message = issue.message.replace(
                str(copy_path), str(path))
            issue.text = issue.text.replace(str(copy_path), str(path))
        return checked_result


def _run_ps_checker(target, exe, allowlist, result) -> CheckResult:
    """チェック役の PowerShell を走らせ、結果を result へ詰める。
    target が実際に ParseFile するファイル(写しのこともある)。"""
    import base64

    encoded = base64.b64encode(_PS_CHECKER_SRC.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ)
    env["SCRIPT_RUNNER_TARGET"] = str(target)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-EncodedCommand", encoded],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=PS_CHECK_TIMEOUT_SECONDS, env=env, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        result.status = STATUS_UNKNOWN
        result.kind = "no_powershell"
        result.headline = "PowerShell でチェックできませんでした"
        return result
    try:
        checked = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        result.status = STATUS_UNKNOWN
        result.kind = "no_powershell"
        result.headline = "PowerShell でチェックできませんでした"
        return result
    errors = checked.get("errors") or []
    if errors:
        result.status = STATUS_ERROR
        result.kind = "syntax"
        result.issues = [
            CheckIssue(line=int(item.get("line") or 0),
                       message=str(item.get("message") or ""),
                       text=str(item.get("text") or ""))
            for item in errors]
        result.jump_line = result.issues[0].line
        result.headline = f"文法エラー {len(errors)}件"
        return result
    resolved = checked.get("resolved") or {}
    warns = []  # (line, message)
    for command in checked.get("commands") or []:
        line = int(command.get("line") or 0)
        name = command.get("name")
        if not name:
            warns.append((line, "動的な呼び出しがあります"))
            continue
        real = str(resolved.get(name) or name)
        if not _ps_allowed(real, allowlist):
            if real.lower() == "remove-item":
                warns.append((line, "ファイルを消す命令があります(Remove-Item)"))
            else:
                warns.append((line, f"{real} は読むだけの許可リストにありません"))
    for redir in checked.get("redirections") or []:
        warns.append((int(redir.get("line") or 0),
                      "ファイルへの書き込み(リダイレクト)があります"))
    for static in checked.get("statics") or []:
        warns.append((int(static.get("line") or 0),
                      f".NET の静的メソッド呼び出しがあります"
                      f"([{static.get('type')}]::{static.get('member')})"))
    for dynamic in checked.get("dynamics") or []:
        warns.append((int(dynamic.get("line") or 0), "動的な呼び出しがあります"))
    for line, message in _ps_member_warns(checked.get("methods")):
        if (line, message) not in warns:
            warns.append((line, message))
    if warns:
        warns.sort(key=lambda item: item[0])
        result.status = STATUS_WARN
        result.kind = "danger"
        result.issues = [CheckIssue(line=line, message=message)
                         for line, message in warns]
        result.jump_line = warns[0][0]
        result.headline = f"要確認・{warns[0][1]}"
        return result
    result.status = STATUS_OK
    result.headline = "問題なし"
    return result


def check_script(path, config: dict) -> CheckResult:
    """拡張子で振り分けるチェックの入口。"""
    path = Path(path)
    if path.suffix.lower() == ".ps1":
        return check_powershell_script(path)
    return check_python_script(path, str(config.get("python") or "python"))


# ---------------------------------------------------------------
# 実行
# ---------------------------------------------------------------
@dataclass
class RunResult:
    name: str = ""
    path: str = ""
    exit_code: object = None    # int。起動できなかったら None
    seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    stopped: bool = False
    policy_stop: bool = False
    sha256: str = ""            # 実行したときの中身
    start_error: str = ""       # 起動できなかった理由


class RunHandle:
    """実行中の子プロセスを止めるための持ち手。窓の「停止」から呼ぶ。"""

    def __init__(self):
        self._proc = None
        self.stopped = False

    def _attach(self, proc):
        self._proc = proc

    def stop(self):
        self.stopped = True
        if self._proc is not None:
            kill_tree(self._proc)


def kill_tree(proc) -> None:
    """子プロセスを木ごと止める(.ps1 から起こした子を残さない)。"""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            flags = subprocess.CREATE_NO_WINDOW
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, creationflags=flags)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _decode_output(raw: bytes) -> str:
    """出力の読み方: UTF-8 で読み、だめなら cp932 で読み直す。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp932", errors="replace")


def detect_policy_stop(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _POLICY_MARKERS)


def detect_policy_stop(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _POLICY_MARKERS)


# ---------------------------------------------------------------
# Job Object(Windows): 時間切れで孫プロセスごと止めるため(中5)
# ---------------------------------------------------------------
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _job_assign(proc):
    """子プロセスを Job Object(KILL_ON_JOB_CLOSE)へ入れる。

    戻り値はジョブのハンドル。Windows でない・失敗したら None。
    本体が終わっても孫プロセスがパイプを持っていると communicate
    が待ち続けるため、時間切れ時はジョブを閉じて木ごと終わらせる。
    テストで差し替えられる継ぎ目(VM では実測できない)。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID,
                                              wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
            wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,
                                                      wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = \
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(
                job, wintypes.HANDLE(proc._handle)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:  # noqa: BLE001  入れられなくても実行は続ける
        return None


def _job_close(job) -> None:
    """ジョブのハンドルを閉じる。KILL_ON_JOB_CLOSE のため、閉じる
    だけで中のプロセスが全部終わる。テストで差し替えられる継ぎ目。"""
    if job is None or sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(job))
    except Exception:  # noqa: BLE001
        pass


def execute_script(path, config: dict, handle: RunHandle = None,
                   expected_sha256: str = None) -> RunResult:
    """スクリプトを1回実行する(ブロッキング。窓は別スレッドから呼ぶ)。

    実行してよいかの判断(F5 かどうか、要確認かどうか)は呼ぶ側の
    仕事。expected_sha256 を渡すと、起動の直前に取り直して比べ、
    違えば実行しない(低8)。記録と結果の sha256 は常に元の
    ファイルのもの。"""
    path = Path(path)
    result = RunResult(name=path.name, path=str(path),
                       sha256=file_sha256(path))
    timeout = _int_or(config.get("timeout_seconds"), 120, 1, 3600)
    is_ps = path.suffix.lower() == ".ps1"
    started = time.monotonic()
    if expected_sha256 is not None:
        current = file_sha256(path)
        result.sha256 = current
        if current != expected_sha256:
            result.seconds = time.monotonic() - started
            result.start_error = \
                "中身が変わっていたため、実行しませんでした"
            result.stderr = result.start_error
            return result
    run_path = path
    bom_copy = None
    if is_ps:
        exe = find_powershell() or "powershell.exe"
        # 高1: BOM 無し UTF-8 は、BOM 付きの写しで実行する。写しは
        # 元と同じフォルダに作る($PSScriptRoot と相対パスが変わら
        # ないため)。sha256 は元のファイルで取る(上で取得済み)。
        bom_raw = ps1_bom_copy_source(path)
        if bom_raw is not None:
            bom_copy = path.parent / (BOM_COPY_PREFIX + path.name)
            try:
                bom_copy.write_bytes(UTF8_BOM + bom_raw)
            except OSError:
                result.seconds = time.monotonic() - started
                result.start_error = (
                    "文字コードの問題で実行できませんでした。"
                    "BOM 付きの UTF-8 で保存してください")
                result.stderr = result.start_error
                return result
            run_path = bom_copy
        argv = [exe, "-NoProfile", "-File", str(run_path)]
        env = dict(os.environ)
    else:
        python = str(config.get("python") or "python")
        argv = [python, str(path)]
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        try:
            proc = subprocess.Popen(
                argv, cwd=str(path.parent), env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=flags)
        except OSError as e:
            result.seconds = time.monotonic() - started
            result.start_error = str(e)
            result.stderr = f"起動できませんでした: {e}"
            return result
        job = _job_assign(proc)
        try:
            if handle is not None:
                handle._attach(proc)
                if handle.stopped:
                    kill_tree(proc)
            try:
                out_raw, err_raw = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as first:
                result.timed_out = True
                out_raw = first.output or b""
                err_raw = first.stderr or b""
                if job is not None:
                    _job_close(job)  # 孫プロセスごと終わらせる
                    job = None
                kill_tree(proc)
                try:
                    out_raw, err_raw = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired as second:
                    # 孫がパイプを持ち続けて回収できなくても、途中
                    # までの出力は捨てない(中5)。
                    if second.output:
                        out_raw = second.output
                    if second.stderr:
                        err_raw = second.stderr
        finally:
            if job is not None:
                _job_close(job)
    finally:
        if bom_copy is not None:
            try:
                bom_copy.unlink()
            except OSError:
                pass
    result.seconds = time.monotonic() - started
    result.exit_code = proc.returncode
    if handle is not None and handle.stopped:
        result.stopped = True
    result.stdout = _decode_output(out_raw or b"")
    result.stderr = _decode_output(err_raw or b"")
    if is_ps and result.exit_code not in (0, None):
        result.policy_stop = detect_policy_stop(
            result.stdout + "\n" + result.stderr)
    return result


# ---------------------------------------------------------------
# クリップボードに入れる文
# ---------------------------------------------------------------
def truncate_text(text: str, head_lines: int = 200, tail_lines: int = 50,
                  max_chars: int = 20000) -> str:
    """長すぎる出力を、先頭と末尾だけにする。

    行数が head+tail を超えたら間に「(中略 N 行)」を入れる。それでも
    全体の文字数が max_chars を超えたら、文字数でも先頭と末尾に切る。
    ちょうど境目のときは何もしない。

    行を数える前に、改行を \n にそろえ、末尾の改行を1つ外す(低7)。
    末尾の改行で空行が1つ増えて数えると、ちょうど境目の行数でも
    中略が入ってしまうため。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.endswith("\n"):
        text = text[:-1]
    lines = text.split("\n")
    if len(lines) > head_lines + tail_lines:
        omitted = len(lines) - head_lines - tail_lines
        kept = lines[:head_lines] + [f"(中略 {omitted} 行)"]
        if tail_lines:
            kept += lines[len(lines) - tail_lines:]
        text = "\n".join(kept)
    if len(text) > max_chars:
        marker = "\n(中略)\n"
        budget = max(0, max_chars - len(marker))
        head = budget // 2
        tail = budget - head
        text = text[:head] + marker + (text[len(text) - tail:] if tail else "")
    return text


def _section(title: str, body: str, config: dict) -> str:
    if not body.strip():
        body = "(なし)"
    else:
        body = truncate_text(
            body, _int_or(config.get("head_lines"), 200, 0, 100000),
            _int_or(config.get("tail_lines"), 50, 0, 100000),
            _int_or(config.get("max_chars"), 20000, 100, 1000000))
    return f"--- {title} ---\n{body}"


def build_result_text(name: str, run: RunResult, config: dict) -> str:
    """実行したときにクリップボードへ入れる文。"""
    config = normalize_config(config)
    if run.timed_out:
        timeout = _int_or(config.get("timeout_seconds"), 120, 1, 3600)
        first = f"[実行結果] {name}  時間切れで止めました({timeout}秒)"
    elif run.stopped:
        first = f"[実行結果] {name}  停止しました"
    else:
        code = run.exit_code if run.exit_code is not None else "-"
        first = f"[実行結果] {name}  終了コード {code}  {run.seconds:.1f}秒"
    if run.start_error:
        first += f"  起動エラー: {run.start_error}"
    parts = [first,
             _section("出力", run.stdout, config),
             _section("エラー出力", run.stderr, config)]
    if run.policy_stop:
        parts.append(POLICY_NOTE)
    return "\n".join(parts)


def build_check_text(name: str, result: CheckResult) -> str:
    """「エラーをコピー」でクリップボードへ入れる文(チェックのエラー用)。"""
    if result.kind == "syntax":
        heading = f"[実行前チェック] {name}  文法エラー {len(result.issues)}件"
    else:
        heading = f"[実行前チェック] {name}  エラー {len(result.issues)}件"
    lines = [heading]
    for issue in result.issues:
        if issue.line:
            lines.append(f"{issue.line}行目: {issue.message}")
        else:
            lines.append(issue.message)
        if issue.text:
            lines.append(f"    {issue.text}")
            if issue.offset:
                lines.append(" " * (4 + issue.offset - 1) + "^")
    lines.append("このエラーを直したスクリプトを、もう一度ください。")
    return "\n".join(lines)


def build_run_preview(run: RunResult) -> str:
    """下半分に出す、直前の結果の要約(先頭の数行だけ)。"""
    total = len(run.stdout.split("\n")) if run.stdout else 0
    if run.timed_out:
        head = "時間切れで止めました"
    elif run.stopped:
        head = "停止しました"
    else:
        code = run.exit_code if run.exit_code is not None else "-"
        head = f"終了コード {code}・{run.seconds:.1f}秒・{total}行"
    lines = [head, "--- 出力 ---"]
    out_lines = run.stdout.split("\n") if run.stdout else []
    shown = out_lines[:5]
    lines.extend(shown)
    if len(out_lines) > len(shown):
        lines.append(f"…あと{len(out_lines) - len(shown)}行")
    if not out_lines:
        lines.append("(なし)")
    lines.append("--- エラー出力 ---")
    lines.append(run.stderr.split("\n")[0] if run.stderr else "(なし)")
    if run.policy_stop:
        lines.append(POLICY_NOTE)
    return "\n".join(lines)


def build_check_preview(result: CheckResult) -> str:
    """下半分に出す、チェック結果の要約(該当行を見せる)。"""
    lines = []
    for issue in result.issues[:5]:
        if issue.line:
            lines.append(f"{issue.line}行目: {issue.message}")
        else:
            lines.append(issue.message)
        if issue.text:
            lines.append(f"    {issue.text}")
            if issue.offset:
                lines.append(" " * (4 + issue.offset - 1) + "^")
    if result.status == STATUS_ERROR and result.kind == "syntax":
        pass
    elif result.status == STATUS_UNKNOWN:
        # チェックできていないのに「文法エラーはありません」は嘘(低6)。
        lines.append("チェックできていません")
    elif result.status == STATUS_WARN:
        lines.append("文法エラーはありません")
    return "\n".join(lines)


# ---------------------------------------------------------------
# 実行の記録
# ---------------------------------------------------------------
def append_run_log(run: RunResult, check: CheckResult = None,
                    log_path=None) -> None:
    """実行の記録を %LOCALAPPDATA%\traytools の下へ1行足す。

    時刻、ファイル名、sha256(実行したときのもの)、チェックの結果、
    終了コード、出力の先頭。職場で「何を走らせたか」を後から
    説明できるようにするためのもの。リポジトリの中には置かない。"""
    if log_path is None:
        log_path = data_dir() / LOG_FILENAME
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "file": run.name,
        "path": run.path,
        "sha256": run.sha256,
        "check": (check.status if check is not None else ""),
        "check_kind": (check.kind if check is not None else ""),
        "exit_code": run.exit_code,
        "seconds": round(run.seconds, 3),
        "timed_out": run.timed_out,
        "stopped": run.stopped,
        "stdout_head": run.stdout[:500],
    }
    try:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 記録できないことは実行の邪魔をしない
