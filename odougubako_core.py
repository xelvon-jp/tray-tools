"""おどうぐばこ (odougubako) の中核ロジック。

AI が作った小物ツールを、フォルダ単位のパッケージとして
登録・実行・記録するための土台。Qt にも tray-tools の他の
モジュールにも依存しない (標準ライブラリだけ) ので、単体で
動かせる。データの置き場は ODOUGUBAKO_DIR で差し替えでき、
既定は %LOCALAPPDATA%\\odougubako。

形式は Agent Skills に寄せている: 道具フォルダには取説の
SKILL.md (名前と説明の frontmatter つき) と scripts/ が入り、
PHAL 拡張の目録 phal-tool.json が入口・引数・由来を持つ。
使用頻度と AI の評価は道具の外の .usage.json に分けて持つ。
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
INDEX_FILENAME = "index.json"
USAGE_FILENAME = ".usage.json"
META_FILENAME = "phal-tool.json"
SKILL_FILENAME = "SKILL.md"
DEFAULT_CATEGORY = "未分類"
DORMANT_DAYS = 90
MIN_USAGE_CHARS = 10
EVAL_KEYS = ("usability", "findability", "expectation")
JST = timezone(timedelta(hours=9))

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]*$")
_DECL_RE = re.compile(r"^#\s*phal-(tool|usage|category)\s*:\s*(.*?)\s*$")
_FOLDER_BAD_RE = re.compile(r'[\\/:*?"<>|]')


def root_dir() -> Path:
    """おどうぐばこのデータフォルダ。"""
    override = os.environ.get("ODOUGUBAKO_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "odougubako"


def _now() -> datetime:
    return datetime.now(JST)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_iso(text):
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def folder_name(text) -> str:
    """フォルダ名に使える形にする (分類名用)。"""
    cleaned = _FOLDER_BAD_RE.sub("_", str(text or "").strip())
    return cleaned or DEFAULT_CATEGORY


def valid_tool_name(name) -> bool:
    return bool(_NAME_RE.match(str(name or "")))


def parse_declarations(source: str):
    """単ファイル登録の宣言行を読む。

    先頭から20行以内にある `# phal-tool:` / `# phal-usage:` /
    `# phal-category:` を拾う。tool 宣言が無ければ None。
    戻り値は {"name", "usage", "category", "body"} で、body は
    宣言行と phal 用の shebang を除いた残り。"""
    lines = str(source).splitlines()
    found = {}
    body_lines = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index == 0 and stripped.startswith("#!"):
            continue
        match = _DECL_RE.match(stripped)
        if match and index < 20:
            found[match.group(1)] = match.group(2)
            continue
        body_lines.append(line)
    if not found.get("tool"):
        return None
    return {
        "name": found["tool"].strip(),
        "usage": (found.get("usage") or "").strip(),
        "category": (found.get("category") or "").strip(),
        "body": "\n".join(body_lines).strip("\n") + "\n",
    }


def extract_docstring(source: str) -> str:
    """モジュールの docstring を取り出す (無ければ空)。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    doc = ast.get_docstring(tree)
    return (doc or "").strip()


def validate_source(source: str, local_modules=()) -> list:
    """道具本体の検証 (登録時に行う)。問題の文字列のリストを
    返す (空なら問題なし)。

    見るのは文法と import の解決だけ。import は、この Python
    から find_spec で見つかるかで判定する。道具自身のフォルダ
    に置かれるモジュール (local_modules) は解決済みとして扱う。"""
    issues = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"文法エラー: {exc.lineno}行目 {exc.msg}"]
    local = {str(m) for m in local_modules}
    missing = []
    import importlib.util

    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                names = [node.module.split(".")[0]]
        for name in names:
            if name in local or name in sys.builtin_module_names:
                continue
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                spec = None
            if spec is None and name not in missing:
                missing.append(name)
    for name in missing:
        issues.append(f"モジュール '{name}' が見つかりません")
    return issues


def _yaml_quote(text) -> str:
    single = " ".join(str(text or "").split())
    return '"' + single.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_skill_md(name: str, usage: str, category: str,
                   body_text: str) -> str:
    """取説 SKILL.md の中身を組み立てる。"""
    body = (body_text or "").strip()
    if not body:
        body = usage
    lines = [
        "---",
        f"name: {name}",
        f"description: {_yaml_quote(usage)}",
        "metadata:",
        f"  category: {_yaml_quote(category)}",
        "---",
        "",
        f"# {name}",
        "",
        body,
        "",
    ]
    return "\n".join(lines)


def read_skill(folder) -> dict:
    """SKILL.md から名前・説明・分類を読む (無ければ空)。"""
    path = Path(folder) / SKILL_FILENAME
    info = {"name": Path(folder).name, "description": "",
            "category": ""}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return info
    if not text.startswith("---"):
        return info
    head = text.split("---", 2)
    if len(head) < 3:
        return info
    for line in head[1].splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            info["name"] = stripped[5:].strip().strip('"')
        elif stripped.startswith("description:"):
            info["description"] = stripped[12:].strip().strip('"')
        elif stripped.startswith("category:"):
            info["category"] = stripped[9:].strip().strip('"')
    return info


def _read_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="")
    tmp.replace(path)


def load_meta(folder) -> dict:
    return _read_json(Path(folder) / META_FILENAME, {})


def load_usage(root=None) -> dict:
    root = Path(root) if root else root_dir()
    return _read_json(root / USAGE_FILENAME, {})


def save_usage(usage: dict, root=None) -> None:
    root = Path(root) if root else root_dir()
    _write_json(root / USAGE_FILENAME, usage)


def usage_of(usage: dict, name: str) -> dict:
    entry = usage.get(name)
    if not isinstance(entry, dict):
        entry = {}
        usage[name] = entry
    entry.setdefault("count", 0)
    entry.setdefault("ok", 0)
    entry.setdefault("fail", 0)
    entry.setdefault("describe_count", 0)
    entry.setdefault("last_used_at", "")
    entry.setdefault("last_args", "")
    entry.setdefault("last_seconds", None)
    entry.setdefault("evals", [])
    return entry


def eval_summary(entry: dict) -> dict:
    """評価の集計 (観点ごとの平均と直近)。"""
    evals = [e for e in entry.get("evals", []) if isinstance(e, dict)]
    summary = {"count": len(evals), "averages": {}, "latest": None,
               "memos": []}
    for key in EVAL_KEYS:
        values = [e["scores"][key] for e in evals
                  if isinstance(e.get("scores"), dict)
                  and isinstance(e["scores"].get(key), (int, float))]
        if values:
            summary["averages"][key] = round(sum(values) / len(values), 2)
    if evals:
        summary["latest"] = evals[-1]
        summary["memos"] = [e.get("memo", "") for e in evals[-3:]
                             if e.get("memo")]
    return summary


def iter_tool_folders(root=None):
    """(分類, 道具名, フォルダ) を列挙する。"""
    root = Path(root) if root else root_dir()
    found = []
    if not root.is_dir():
        return found
    for category_dir in sorted(root.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith("."):
            continue
        for tool_dir in sorted(category_dir.iterdir()):
            if tool_dir.is_dir() and (tool_dir / META_FILENAME).is_file():
                found.append((category_dir.name, tool_dir.name, tool_dir))
    return found


def find_tool(name, root=None):
    """道具名からフォルダを探す (無ければ None)。"""
    for _category, tool_name, folder in iter_tool_folders(root):
        if tool_name == name:
            return folder
    return None


def tool_info(category, name, folder, usage) -> dict:
    meta = load_meta(folder)
    skill = read_skill(folder)
    entry = usage_of(dict(usage), name) if usage is not None else {}
    summary = eval_summary(entry)
    last_used = _parse_iso(entry.get("last_used_at"))
    registered = _parse_iso(meta.get("registered_at"))
    base = last_used or registered
    dormant = bool(base and (_now() - base).days >= DORMANT_DAYS
                    and entry.get("count", 0) >= 0)
    count = int(entry.get("count", 0))
    ok = int(entry.get("ok", 0))
    return {
        "name": name,
        "category": skill.get("category") or category,
        "description": skill.get("description", ""),
        "version": int(meta.get("version", 1)),
        "entry": meta.get("entry", ""),
        "runtime": meta.get("runtime", "python"),
        "created_by": meta.get("created_by", ""),
        "created_from": meta.get("created_from", ""),
        "derived_from": meta.get("derived_from"),
        "registered_at": meta.get("registered_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "folder": str(folder),
        "has_prev": (Path(folder) / ".prev").is_dir(),
        "count": count,
        "ok": ok,
        "fail": int(entry.get("fail", 0)),
        "success_rate": (round(ok / count, 2) if count else None),
        "describe_count": int(entry.get("describe_count", 0)),
        "last_used_at": entry.get("last_used_at", ""),
        "last_seconds": entry.get("last_seconds"),
        "eval": summary,
        "dormant": dormant,
    }


def rebuild_index(root=None) -> dict:
    """index.json を作り直して返す。"""
    root = Path(root) if root else root_dir()
    usage = load_usage(root)
    tools = [tool_info(cat, name, folder, usage)
             for cat, name, folder in iter_tool_folders(root)]
    tools.sort(key=lambda t: (t["category"], -t["count"], t["name"]))
    index = {"schema_version": SCHEMA_VERSION,
             "generated_at": _now_iso(), "tools": tools}
    _write_json(root / INDEX_FILENAME, index)
    return index


def list_tools(root=None) -> list:
    return rebuild_index(root)["tools"]


def register_source(source: str, created_from: str = "",
                    created_by: str = "", root=None) -> dict:
    """単ファイルの登録 (宣言つき .phal) を取り込む。

    検証を通れば即時に有効化する。戻り値は
    {"ok": bool, "reason": str, "tool": dict|None}。
    上書きのときは旧版を道具フォルダの .prev に1世代残す。"""
    root = Path(root) if root else root_dir()
    decl = parse_declarations(source)
    if decl is None:
        return {"ok": False, "reason": "phal-tool 宣言がありません",
                "tool": None}
    name = decl["name"]
    if not valid_tool_name(name):
        return {"ok": False,
                "reason": f"道具名 '{name}' が使えません"
                          " (半角英数字と _-. のみ)",
                "tool": None}
    usage = decl["usage"]
    if len(usage) < MIN_USAGE_CHARS:
        return {"ok": False,
                "reason": f"説明が短すぎます ({MIN_USAGE_CHARS}文字以上で、"
                          "何をするかと、いつ使うかを書いてください)",
                "tool": None}
    body = decl["body"]
    issues = validate_source(body)
    if issues:
        return {"ok": False, "reason": " / ".join(issues), "tool": None}
    category = folder_name(decl["category"] or DEFAULT_CATEGORY)
    root.mkdir(parents=True, exist_ok=True)
    folder = root / category / name
    existing = find_tool(name, root)
    previous_version = 0
    if existing is not None:
        previous_version = int(load_meta(existing).get("version", 1))
        prev_dir = existing / ".prev"
        if prev_dir.exists():
            shutil.rmtree(prev_dir)
        prev_dir.mkdir(parents=True)
        for item in existing.iterdir():
            if item.name == ".prev":
                continue
            if item.is_dir():
                shutil.copytree(item, prev_dir / item.name)
            else:
                shutil.copy2(item, prev_dir / item.name)
        if existing != folder:
            shutil.rmtree(existing)
    folder.mkdir(parents=True, exist_ok=True)
    scripts = folder / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "main.py").write_text(body, encoding="utf-8", newline="")
    docstring = extract_docstring(body)
    skill_text = build_skill_md(name, usage, decl["category"] or category,
                                docstring)
    (folder / SKILL_FILENAME).write_text(skill_text, encoding="utf-8",
                                         newline="")
    stamp = _now_iso()
    meta = {
        "schema_version": SCHEMA_VERSION,
        "entry": "scripts/main.py",
        "runtime": "python",
        "args": [],
        "created_by": created_by,
        "created_from": created_from,
        "derived_from": None,
        "version": previous_version + 1,
        "registered_at": stamp,
        "updated_at": stamp,
    }
    if existing is not None:
        old_meta = load_meta(existing) if existing == folder else meta
        meta["registered_at"] = old_meta.get("registered_at", stamp)
    _write_json(folder / META_FILENAME, meta)
    rebuild_index(root)
    info = None
    for tool in list_tools(root):
        if tool["name"] == name:
            info = tool
            break
    return {"ok": True, "reason": "", "tool": info}


def describe(name, root=None) -> str:
    """道具の取説 (SKILL.md) を返す。取得を頻度に数える。"""
    root = Path(root) if root else root_dir()
    folder = find_tool(name, root)
    if folder is None:
        return ""
    usage = load_usage(root)
    usage_of(usage, name)["describe_count"] += 1
    save_usage(usage, root)
    try:
        return (folder / SKILL_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return ""


def record_run(name, ok: bool, seconds: float, args_summary: str = "",
               root=None) -> None:
    root = Path(root) if root else root_dir()
    usage = load_usage(root)
    entry = usage_of(usage, name)
    entry["count"] += 1
    entry["ok" if ok else "fail"] += 1
    entry["last_used_at"] = _now_iso()
    entry["last_args"] = str(args_summary or "")[:120]
    entry["last_seconds"] = round(float(seconds), 3)
    save_usage(usage, root)


def record_eval(name, scores: dict, memo: str = "",
                command_id: str = "", root=None) -> dict:
    """AI の自己採点を記録する。scores は EVAL_KEYS の 1〜5。"""
    root = Path(root) if root else root_dir()
    cleaned = {}
    for key in EVAL_KEYS:
        value = scores.get(key) if isinstance(scores, dict) else None
        if isinstance(value, (int, float)) and 1 <= value <= 5:
            cleaned[key] = int(value)
    if not cleaned:
        return {"ok": False, "reason": "点数が1つも読み取れませんでした"}
    usage = load_usage(root)
    entry = usage_of(usage, name)
    entry["evals"].append({
        "at": _now_iso(), "scores": cleaned,
        "memo": str(memo or "")[:200], "command_id": str(command_id or ""),
    })
    entry["evals"] = entry["evals"][-50:]
    save_usage(usage, root)
    return {"ok": True, "reason": ""}


def _validate_args(meta: dict, args: list) -> str:
    """引数定義に対する軽い検証。問題があれば理由を返す。"""
    defs = meta.get("args") or []
    if not defs:
        return ""
    required = [d for d in defs if d.get("required")]
    if len(args) < len(required):
        return f"引数が足りません ({len(required)}個以上必要です)"
    for definition, value in zip(defs, args):
        kind = definition.get("type")
        if kind == "path" and not Path(value).exists():
            return f"パスが実在しません: {value}"
        if kind == "int":
            try:
                int(value)
            except (TypeError, ValueError):
                return f"数値が必要です: {value}"
    return ""


def run_tool(name, args=(), timeout: float = 120.0, root=None) -> dict:
    """道具を名前で実行する。実測 (時間・成否) を頻度に残す。

    戻り値は {"ok", "exit_code", "stdout", "stderr", "seconds",
    "reason"}。ok は終了コード 0 のときだけ True。"""
    root = Path(root) if root else root_dir()
    folder = find_tool(name, root)
    if folder is None:
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": "", "seconds": 0.0,
                "reason": f"道具 '{name}' がおどうぐばこにありません"}
    meta = load_meta(folder)
    problem = _validate_args(meta, list(args))
    if problem:
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": "", "seconds": 0.0, "reason": problem}
    entry_path = folder / str(meta.get("entry") or "scripts/main.py")
    if not entry_path.is_file():
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": "", "seconds": 0.0,
                "reason": f"入口ファイルがありません: {entry_path}"}
    command = [sys.executable, str(entry_path)] + [str(a) for a in args]
    env = dict(os.environ)
    here = str(Path(__file__).resolve().parent)
    pythonpath = [str(folder), str(folder / "scripts"), here]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    started = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=str(folder), env=env,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout)
        seconds = time.monotonic() - started
        ok = proc.returncode == 0
        record_run(name, ok, seconds, " ".join(str(a) for a in args), root)
        return {"ok": ok, "exit_code": proc.returncode,
                "stdout": proc.stdout or "", "stderr": proc.stderr or "",
                "seconds": round(seconds, 3), "reason": ""}
    except subprocess.TimeoutExpired:
        seconds = time.monotonic() - started
        record_run(name, False, seconds, " ".join(str(a) for a in args),
                   root)
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": "", "seconds": round(seconds, 3),
                "reason": f"{timeout:g}秒でタイムアウトしました"}


def rollback(name, root=None) -> dict:
    """前版 (.prev) と現行を入れ替える。"""
    root = Path(root) if root else root_dir()
    folder = find_tool(name, root)
    if folder is None:
        return {"ok": False, "reason": f"道具 '{name}' がありません"}
    prev_dir = folder / ".prev"
    if not prev_dir.is_dir():
        return {"ok": False, "reason": "前版がありません"}
    swap = folder / ".swap"
    if swap.exists():
        shutil.rmtree(swap)
    swap.mkdir()
    for item in list(folder.iterdir()):
        if item.name in (".prev", ".swap"):
            continue
        shutil.move(str(item), str(swap / item.name))
    for item in list(prev_dir.iterdir()):
        shutil.move(str(item), str(folder / item.name))
    for item in list(swap.iterdir()):
        shutil.move(str(item), str(prev_dir / item.name))
    swap.rmdir()
    meta = load_meta(folder)
    meta["updated_at"] = _now_iso()
    _write_json(folder / META_FILENAME, meta)
    rebuild_index(root)
    return {"ok": True, "reason": ""}


def remove_tool(name, root=None) -> dict:
    """道具を消す (人が行う操作)。頻度の記録も消す。"""
    root = Path(root) if root else root_dir()
    folder = find_tool(name, root)
    if folder is None:
        return {"ok": False, "reason": f"道具 '{name}' がありません"}
    shutil.rmtree(folder)
    usage = load_usage(root)
    if name in usage:
        del usage[name]
        save_usage(usage, root)
    rebuild_index(root)
    return {"ok": True, "reason": ""}
