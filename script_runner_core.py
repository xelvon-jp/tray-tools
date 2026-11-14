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
import re
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
# .phal はループ用の独自拡張子。1行目の宣言(#!phal python /
# #!phal powershell)で中身の言語を決める(phal_engine)。
TARGET_EXTENSIONS = (".py", ".ps1", ".phal")

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
    "summary_mode": True,      # 長い出力は要約にして全文をファイルへ置く
    "summary_lines": 100,      # 要約に切り替える行数(出力+エラー出力の合計)
    "summary_chars": 8000,     # 要約に切り替える文字数(出力+エラー出力の合計)
    "window_geometry": None,   # [x, y, w, h]
    "always_on_top": False,    # 前面に固定
    "hide_no_id": True,        # ID が無いファイルを表示しない
    "dark_mode": False,        # ダークモード (窓の中の「ダーク」の切替)
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
    config["summary_lines"] = _int_or(config["summary_lines"], 100, 10, 100000)
    config["summary_chars"] = _int_or(config["summary_chars"], 8000, 100, 1000000)
    geometry = config["window_geometry"]
    if not (isinstance(geometry, list) and len(geometry) == 4
            and all(isinstance(v, int) for v in geometry)):
        config["window_geometry"] = None
    config["always_on_top"] = bool(config["always_on_top"])
    config["hide_no_id"] = bool(config["hide_no_id"])
    config["dark_mode"] = bool(config["dark_mode"])
    config["summary_mode"] = bool(config["summary_mode"])
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


def cleanup_bom_copies(folder) -> int:
    """フォルダに残った BOM 付け写し(.sr-bom-*)を消す(低2)。

    写しは実行後に消す作りだが、本体が強制終了されると残る。
    パネルの起動直後は実行中のものは無いので、残りは全部消して
    よい。消せなかったものは無視して進む。戻り値は消せた数。"""
    removed = 0
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(BOM_COPY_PREFIX):
            continue
        try:
            if entry.is_file():
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


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

    def to_dict(self) -> dict:
        """キャッシュへ残すための辞書化。画面に出す欄は全部残す。"""
        return {
            "status": self.status,
            "kind": self.kind,
            "issues": [
                {"line": issue.line, "message": issue.message,
                 "text": issue.text, "offset": issue.offset}
                for issue in self.issues],
            "headline": self.headline,
            "jump_line": self.jump_line,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data):
        issues = [
            CheckIssue(line=int(entry.get("line", 0)),
                       message=str(entry.get("message", "")),
                       text=str(entry.get("text", "")),
                       offset=int(entry.get("offset", 0)))
            for entry in data.get("issues", [])
            if isinstance(entry, dict)]
        return cls(
            status=str(data.get("status", STATUS_UNKNOWN)),
            kind=str(data.get("kind", "")),
            issues=issues,
            headline=str(data.get("headline", "")),
            jump_line=int(data.get("jump_line", 0)),
            sha256=str(data.get("sha256", "")),
        )


# ---------------------------------------------------------------
# チェック結果のキャッシュ (再起動をまたいで効かせる)
# ---------------------------------------------------------------
# 実行ログ (script_runner_log.jsonl) と同じ置き場に、チェック済みの
# 結果を控える。キーは (パス, 更新時刻, 大きさ)。中身が変われば
# 更新時刻か大きさが変わるので外れる。控えの sha256 はチェックした
# 時点のもので、実行直前の取り直し (窓側) が最後の砦として残る。
CHECK_CACHE_FILENAME = "script_runner_check_cache.json"
CHECK_CACHE_LIMIT = 200


def check_cache_path() -> Path:
    return data_dir() / CHECK_CACHE_FILENAME


def load_check_cache(cache_path=None) -> dict:
    """キャッシュ {パス文字列: 控え} を読み込む。

    壊れている・読めない場合は空として扱う (キャッシュが理由で
    パネルが落ちることがあってはならない)。"""
    path = Path(cache_path) if cache_path is not None else check_cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cache = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("result"), dict):
            continue
        cache[key] = entry
    return cache


def save_check_cache(cache: dict, cache_path=None) -> None:
    """キャッシュを書き出す。失敗しても黙って諦める (次に開いたとき
    チェックし直すだけ)。書きかけで壊さないよう、一度別名で書いて
    から置き換える。"""
    path = Path(cache_path) if cache_path is not None else check_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def check_cache_get(cache: dict, path, mtime, size):
    """(パス, 更新時刻, 大きさ) が一致すれば控えの結果を返す。

    一致しなければ None (チェックし直す)。"""
    entry = cache.get(str(path))
    if not isinstance(entry, dict):
        return None
    if entry.get("mtime") != mtime or entry.get("size") != size:
        return None
    try:
        return CheckResult.from_dict(entry["result"])
    except (KeyError, TypeError, ValueError):
        return None


def check_cache_put(cache: dict, path, mtime, size,
                    result: CheckResult) -> None:
    """結果を控える。上限を超えたら古い控えから落とす。"""
    cache[str(path)] = {
        "mtime": mtime,
        "size": size,
        "saved_at": time.time(),
        "result": result.to_dict(),
    }
    while len(cache) > CHECK_CACHE_LIMIT:
        oldest = min(cache, key=lambda k: cache[k].get("saved_at", 0))
        del cache[oldest]


# ---------------------------------------------------------------
# 自動実行の対象の控え (script_runner_pending_runs.json)。
# チェックの控えと同じ置き場に、「自動実行の対象になったファイル」
# の素性 (パス・更新時刻・大きさ・分かれば sha256) を残す。PHAL が
# 開き直すと、起動スナップショットだけでは「起動後に届いた分」と
# 分からなくなる (ダウンロード直後に開き直した場合に、その分が
# 起動前からの滞留扱いになって自動で走らなくなる)。控えが今の
# 中身と一致する間は、開き直した後も対象のままにする。控えが
# 無い滞留分は、従来どおり決して対象にしない。
PENDING_RUN_FILENAME = "script_runner_pending_runs.json"
PENDING_RUN_TTL_SECONDS = 24 * 3600
PENDING_RUN_LIMIT = 200


def pending_run_path() -> Path:
    return data_dir() / PENDING_RUN_FILENAME


def _pending_run_expired(entry, now: float) -> bool:
    saved_at = entry.get("saved_at") if isinstance(entry, dict) else None
    return not isinstance(saved_at, (int, float)) \
        or now - saved_at > PENDING_RUN_TTL_SECONDS


def load_pending_runs(records_path=None) -> dict:
    """控え {パス文字列: 控え} を読み込む。

    壊れている・読めない場合は空として扱う (チェックの控えと
    同じく、控えが理由でパネルが落ちてはならない)。古い控え
    (24時間超) は、読み込みの時点で落とす。"""
    path = Path(records_path) if records_path is not None \
        else pending_run_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    records = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("mtime"), (int, float)):
            continue
        if not isinstance(entry.get("size"), int):
            continue
        if _pending_run_expired(entry, now):
            continue
        records[key] = entry
    return records


def save_pending_runs(records: dict, records_path=None) -> None:
    """控えを書き出す。失敗しても黙って諦める (次に開いたとき
    対象から外れるだけ)。書きかけで壊さないよう、一度別名で書いて
    から置き換える。"""
    path = Path(records_path) if records_path is not None \
        else pending_run_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(records, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def pending_run_put(records: dict, path, mtime, size,
                    sha256: str = "") -> None:
    """対象になったファイルの素性を控える。古い控えと上限超えは
    ここで落とす (ファイルは小さく保つ)。"""
    records[str(path)] = {
        "mtime": mtime,
        "size": size,
        "sha256": sha256 or "",
        "saved_at": time.time(),
    }
    now = time.time()
    for key in [k for k, e in records.items()
                if _pending_run_expired(e, now)]:
        del records[key]
    while len(records) > PENDING_RUN_LIMIT:
        oldest = min(records, key=lambda k: records[k].get("saved_at", 0))
        del records[oldest]


def pending_run_get(records: dict, path, mtime, size):
    """(パス, 更新時刻, 大きさ) が一致すれば控えを返す。

    一致しない・古すぎる場合は None (対象にはしない)。"""
    entry = records.get(str(path))
    if not isinstance(entry, dict):
        return None
    if entry.get("mtime") != mtime or entry.get("size") != size:
        return None
    if _pending_run_expired(entry, time.time()):
        return None
    return entry


def pending_run_discard(records: dict, path) -> bool:
    """パスの控えを捨てる。捨てた (保存が要る) なら True。"""
    return records.pop(str(path), None) is not None


# ---------------------------------------------------------------
# DL 済み候補の控え (script_runner_dl_consumed.json)。
# チャットで見つけたダウンロード候補を押した記録を、チェックの
# 控えと同じ置き場に残す。パネルが開き直すと会話内の控え
# (押下済みの集合) が消えるので、開き直した先でも「もう落とした
# 候補」と分かるようにするため (1-27 の実機で、設置の開き直しの
# たびに同じ候補を落とし直す無限ループになった)。キーは候補の
# 同一判定 (名前 + クラス) で、概要も一緒に持つ。24時間を超えた
# 控えは読み込み・追加の時点で落とす。
DL_CONSUMED_FILENAME = "script_runner_dl_consumed.json"
DL_CONSUMED_TTL_SECONDS = 24 * 3600
DL_CONSUMED_LIMIT = 100


def dl_consumed_path() -> Path:
    return data_dir() / DL_CONSUMED_FILENAME


def _dl_consumed_expired(entry, now: float) -> bool:
    saved_at = entry.get("saved_at") if isinstance(entry, dict) else None
    return not isinstance(saved_at, (int, float)) \
        or now - saved_at > DL_CONSUMED_TTL_SECONDS


def load_dl_consumed(records_path=None) -> dict:
    """控え {候補の key: 控え} を読み込む。

    壊れている・読めない場合は空として扱う (他の控えと同じく、
    控えが理由でパネルが落ちてはならない)。古い控え (24時間超)
    は、読み込みの時点で落とす。"""
    path = Path(records_path) if records_path is not None \
        else dl_consumed_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    records = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        if _dl_consumed_expired(entry, now):
            continue
        records[key] = entry
    return records


def save_dl_consumed(records: dict, records_path=None) -> None:
    """控えを書き出す。失敗しても黙って諦める (次に開いたとき
    候補が出直すだけ)。書きかけで壊さないよう、一度別名で書いて
    から置き換える。"""
    path = Path(records_path) if records_path is not None \
        else dl_consumed_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(records, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def dl_consumed_key(name, class_name) -> str:
    """候補の同一判定のキー (窓側の _DlCandidate.key と同じ形)。"""
    return f"{name}\n{class_name}"


def dl_consumed_put(records: dict, name, class_name,
                    summary: str = "") -> None:
    """押下を始めた候補を控える。古い控えと上限超えはここで落とす
    (ファイルは小さく保つ)。"""
    records[dl_consumed_key(name, class_name)] = {
        "name": name or "",
        "class_name": class_name or "",
        "summary": summary or "",
        "saved_at": time.time(),
    }
    now = time.time()
    for key in [k for k, e in records.items()
                if _dl_consumed_expired(e, now)]:
        del records[key]
    while len(records) > DL_CONSUMED_LIMIT:
        oldest = min(records, key=lambda k: records[k].get("saved_at", 0))
        del records[oldest]


def dl_consumed_get(records: dict, name, class_name):
    """候補が控えにあり、まだ新しい (24時間以内) なら控えを返す。

    無ければ None。"""
    entry = records.get(dl_consumed_key(name, class_name))
    if not isinstance(entry, dict):
        return None
    if _dl_consumed_expired(entry, time.time()):
        return None
    return entry


# ---------------------------------------------------------------
# 実行済みハッシュの控え (script_runner_executed_hashes.json)。
# 実行を始めたスクリプトの中身の sha256 を控える。自動実行は、
# ここにある中身を二度は走らせない (1-29、Nagato の提案)。
# ブラウザが同じファイルを (1) (2) と写しで落としても、写しは
# 別ファイルでも中身が同じなので、この控えで止められる。
# 実行ログ (script_runner_log.jsonl) にも sha256 は残るが、あちら
# は監査用の積み上げで際限なく伸びる。自動実行の判定は毎回引く
# ので、上限のある小さな控えを別に持つ (チェックの控え・対象の
# 控えと同じ作法)。7日を超えた控えは読み込み・追加で落とす。
EXECUTED_HASH_FILENAME = "script_runner_executed_hashes.json"
EXECUTED_HASH_TTL_SECONDS = 7 * 24 * 3600
EXECUTED_HASH_LIMIT = 200


def executed_hash_path() -> Path:
    return data_dir() / EXECUTED_HASH_FILENAME


def _executed_hash_expired(saved_at, now: float) -> bool:
    return not isinstance(saved_at, (int, float)) \
        or now - saved_at > EXECUTED_HASH_TTL_SECONDS


def load_executed_hashes(records_path=None) -> dict:
    """控え {sha256: 実行した時刻} を読み込む。

    壊れている・読めない場合は空として扱う。古い控え (7日超) は、
    読み込みの時点で落とす。"""
    path = Path(records_path) if records_path is not None \
        else executed_hash_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    records = {}
    for sha, saved_at in raw.items():
        if not isinstance(sha, str) or not sha:
            continue
        if _executed_hash_expired(saved_at, now):
            continue
        records[sha] = saved_at
    return records


def save_executed_hashes(records: dict, records_path=None) -> None:
    """控えを書き出す。失敗しても黙って諦める (自動実行の歯止めが
    1回分緩むだけ)。書きかけで壊さないよう、一度別名で書いてから
    置き換える。"""
    path = Path(records_path) if records_path is not None \
        else executed_hash_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(records, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def executed_hash_put(records: dict, sha256: str) -> None:
    """実行を始めた中身の sha256 を控える。古い控えと上限超えは
    ここで落とす。"""
    if not sha256:
        return
    records[sha256] = time.time()
    now = time.time()
    for sha in [s for s, t in records.items()
                if _executed_hash_expired(t, now)]:
        del records[sha]
    while len(records) > EXECUTED_HASH_LIMIT:
        oldest = min(records, key=lambda s: records[s])
        del records[oldest]


def executed_hash_contains(records: dict, sha256: str) -> bool:
    """その中身が実行済みの控えにあるか (7日以内)。"""
    if not sha256:
        return False
    saved_at = records.get(sha256)
    return not _executed_hash_expired(saved_at, time.time())


# ---------------------------------------------------------------
# 診断の控え (script_runner_diag.json)。
# ループが止まったとき、画面の外からは「どの段で・なぜ」止まった
# のかが見えない。PHAL 自身の直近の動き (DL の押下・チェック・
# 実行・回答・送信・開き直し・自動実行の見送り) を短い文で控えて
# おき、「AIの窓」の「診断をAIへ貼る」でまとめて AI の入力欄へ
# 入れられるようにする (1-41、Nagato の提案)。控えるのは PHAL が
# 既に画面に出している名前と結果の文だけで、ファイルの中身や
# クリップボードは入れない。開き直しをまたいで残すため、他の
# 控えと同じ置き場に小さな JSON で置く。
DIAG_FILENAME = "script_runner_diag.json"
DIAG_LIMIT = 60               # 控える件数の上限
DIAG_TEXT_LIMIT = 240         # 1件の文の長さの上限 (文字)
DIAG_BUNDLE_EVENT_COUNT = 25  # 束に入れる直近の件数
DIAG_BUNDLE_LIMIT = 4000      # 束全体の長さの上限 (文字)


def diag_path() -> Path:
    return data_dir() / DIAG_FILENAME


def load_diag_events(events_path=None) -> list:
    """控え [{at: 時刻, text: 文}] を読み込む (古い順)。

    壊れている・読めない場合は空として扱う (診断が理由でパネルが
    落ちてはならない)。形の違う要素は読み飛ばし、上限を超えて
    いれば新しい方だけ残す。"""
    path = Path(events_path) if events_path is not None else diag_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    events = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        at = entry.get("at")
        text = entry.get("text")
        if not isinstance(at, (int, float)) \
                or not isinstance(text, str) or not text:
            continue
        events.append({"at": float(at), "text": text})
    return events[-DIAG_LIMIT:]


def save_diag_events(events: list, events_path=None) -> None:
    """控えを書き出す。失敗しても黙って諦める (次に開いたとき
    診断が少し古いだけ)。書きかけで壊さないよう、一度別名で
    書いてから置き換える。"""
    path = Path(events_path) if events_path is not None else diag_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(events[-DIAG_LIMIT:], f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def diag_append(events: list, text: str, at=None) -> None:
    """1件足す (その場で書き換える)。上限を超えたら古い方から
    落とす。文は1行に畳んで長さを切る (押下の結果の文など、
    そのままでは長いものがあるため)。"""
    text = " ".join(str(text).split())
    if not text:
        return
    if len(text) > DIAG_TEXT_LIMIT:
        text = text[:DIAG_TEXT_LIMIT - 1] + "…"
    events.append({
        "at": float(at) if at is not None else time.time(),
        "text": text,
    })
    del events[:-DIAG_LIMIT]


def _diag_time_text(at: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(at))


def build_diag_bundle(version: str, toggles: dict, profile_name: str,
                      chain_lines, events, at=None) -> str:
    """AI へ貼る診断の束を組み立てる。

    見出し (PHAL の版と時刻)・自動化の切替・有効なプロファイル名・
    今の連鎖の状態・直近の記録 (古い順) を、短い平文で並べる。
    全体の上限を超える場合は、記録の古い方から落として収める
    (見出しと状態は落とさない)。"""
    now = float(at) if at is not None else time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    def on_off(key):
        return "入" if (toggles or {}).get(key) else "切"

    head = [
        f"PHAL診断 Ver{version} {stamp}",
        f"自動化: DL={on_off('auto_dl')} 実行={on_off('auto_run')} "
        f"回答={on_off('auto_answer')} 送信={on_off('auto_send')} "
        f"診断={on_off('auto_diag')}",
        f"AIプロファイル: {profile_name or '(未設定)'}",
        "状態:",
    ]
    head += [f"- {line}" for line in (chain_lines or [])]
    head.append("直近の記録:")
    recent = list(events or [])[-DIAG_BUNDLE_EVENT_COUNT:]
    lines = [f"{_diag_time_text(e['at'])} {e['text']}" for e in recent]
    while lines and len("\n".join(head + lines)) > DIAG_BUNDLE_LIMIT:
        lines.pop(0)
    bundle = "\n".join(head + lines)
    if len(bundle) > DIAG_BUNDLE_LIMIT:
        bundle = bundle[:DIAG_BUNDLE_LIMIT]
    return bundle


# ---------------------------------------------------------------
# 診断の自動応答の控え (script_runner_diag_auto.json)。
# AI の発言の中の目印の行 (copilot_loop.DIAG_REQUEST_MARKER) を
# 見つけたら、診断の束を自動で入れて送る機能 (1-43) の歯止め。
# 同じ発言で二度送らないための「消費済みの目印の ID」と、短時間
# に繰り返さないための「直近に送った時刻」を、開き直しをまたいで
# 残す。他の控えと同じ置き場・同じ作法 (壊れたら空に倒す)。
DIAG_AUTO_FILENAME = "script_runner_diag_auto.json"
DIAG_AUTO_COOLDOWN_SEC = 300.0  # 自動応答の間隔 (5分)


def diag_auto_path() -> Path:
    return data_dir() / DIAG_AUTO_FILENAME


def load_diag_auto(state_path=None) -> dict:
    """自動応答の控え {consumed_id, last_sent_at} を読み込む。

    壊れている・読めない・形が違う場合は、消費した物は無く
    送ったことも無い、として扱う (自動応答が理由でパネルが
    落ちてはならない)。"""
    path = Path(state_path) if state_path is not None \
        else diag_auto_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"consumed_id": "", "last_sent_at": 0.0}
    if not isinstance(raw, dict):
        return {"consumed_id": "", "last_sent_at": 0.0}
    consumed = raw.get("consumed_id")
    sent_at = raw.get("last_sent_at")
    return {
        "consumed_id": consumed if isinstance(consumed, str) else "",
        "last_sent_at": float(sent_at)
        if isinstance(sent_at, (int, float)) else 0.0,
    }


def save_diag_auto(state: dict, state_path=None) -> None:
    """自動応答の控えを書き出す。失敗しても黙って諦める (次に
    開いたとき控えが少し古いだけ)。書きかけで壊さないよう、
    一度別名で書いてから置き換える。"""
    path = Path(state_path) if state_path is not None \
        else diag_auto_path()
    state = state if isinstance(state, dict) else {}
    payload = {
        "consumed_id": str(state.get("consumed_id") or ""),
        "last_sent_at": float(state.get("last_sent_at") or 0.0),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------
# PHAL の状態の書き出し (script_runner_status.json ではなく
# phal_status.json。ARiA モニタ表示用、req-20261115-02)。
# PHAL の今の段を、%LOCALAPPDATA%\traytools の下の小さな JSON
# へ出す。見張り(アオイ側)がこれを読んで、モニタに PHAL の
# カードを描く。書くのは状態が変わったときだけ。書き込みに
# 失敗しても、ループは止めない (他の控えと同じ作法)。
# ---------------------------------------------------------------
PHAL_STATUS_FILENAME = "phal_status.json"
PHAL_STATUS_STATES = ("idle", "dl", "run", "answer", "send", "stopped")
_PHAL_STATUS_TEXT_LIMIT = 240  # detail / item の1行の長さの上限(文字)


def phal_status_path() -> Path:
    return data_dir() / PHAL_STATUS_FILENAME


def _phal_status_one_line(text) -> str:
    """detail / item 用に、1行に畳んで長さを切る。"""
    flat = " ".join(str(text or "").split())
    if len(flat) > _PHAL_STATUS_TEXT_LIMIT:
        flat = flat[:_PHAL_STATUS_TEXT_LIMIT - 1] + "…"
    return flat


def build_phal_status(version, state, command_id="", detail="",
                      stopped_reason="", step=0, item="", at=None) -> dict:
    """モニタ表示用の状態の辞書を作る。

    state は idle(待機) / dl(ダウンロード・新着待ち) / run(実行中)
    / answer(回答中) / send(送信中) / stopped(止まった) のどれか。
    step は PHAL 起動後の実行手数。item は実行中または直近の
    スクリプトの概要。updated は epoch 秒(整数)。知らない state
    は ValueError (窓側は決まった段だけを渡す)。"""
    if state not in PHAL_STATUS_STATES:
        raise ValueError(f"unknown PHAL state: {state!r}")
    return {
        "schema": 1,
        "version": str(version),
        "state": state,
        "command_id": str(command_id or ""),
        "detail": _phal_status_one_line(detail),
        "stopped_reason": _phal_status_one_line(stopped_reason),
        "step": int(step),
        "item": _phal_status_one_line(item),
        "updated": int(at if at is not None else time.time()),
    }


def _phal_status_body(status: dict) -> dict:
    """書き直すかどうかの比べる部分。updated は毎回変わる時刻
    なので比べない (比べると、同じ状態でも毎回書き直すことに
    なる)。"""
    return {key: value for key, value in status.items()
            if key != "updated"}


def write_phal_status(status: dict, status_path=None) -> bool:
    """状態を書き出す。変わったときだけ書き、書けたら True。

    前回書いた内容(時刻を除く)と同じなら書かずに False を返す。
    書きかけで壊さないよう、一度別名で書いてから置き換える
    (他の控えと同じ作法)。書き込みに失敗しても例外は出さず
    False を返す (ループを止めない)。"""
    path = Path(status_path) if status_path is not None \
        else phal_status_path()
    body = _phal_status_body(status)
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
        if isinstance(prev, dict) and _phal_status_body(prev) == body:
            return False
    except (OSError, ValueError):
        pass  # 読めない・壊れている場合は書き直す
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(status, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


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
import importlib.util
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


# モジュールがあるかどうか(対象を読み込むことはしない)。
# 組み込みモジュール(sys・time・math・itertools など)は、探す場所に
# 実ファイルが無いため PathFinder では見つからない。名前で先に除く。
# 次に探す場所(スクリプトのフォルダ+sys.path)を PathFinder で調べ、
# 最後の保険にパス指定なしの find_spec でも試す(二段構え)。
def _module_exists(name, search_path):
    if name in sys.builtin_module_names:
        return True
    try:
        if PathFinder.find_spec(name, search_path) is not None:
            return True
    except Exception:
        pass
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


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
        if not _module_exists(name, search_path):
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


# PowerShell の警告対象 (実害のある命令だけ)。解決済みの名前
# (エイリアスを直したもの)で引くので、rm や del も Remove-Item に
# 集約される。許可リスト方式は 2026-11-14 の緩和でやめた。
_PS_DANGER_COMMANDS = {
    "remove-item": "ファイルを消す命令があります",
    "rename-item": "ファイル名を変える命令があります",
    "move-item": "ファイルを移す命令があります",
    "set-content": "ファイルへ書き込む命令があります",
    "add-content": "ファイルへ書き込む命令があります",
    "clear-content": "ファイルの中身を消す命令があります",
    "out-file": "ファイルへ書き込む命令があります",
    "invoke-expression": "文字列を実行する命令があります",
    "invoke-command": "動的に実行する命令があります",
    "invoke-webrequest": "外部へ通信する命令があります",
    "invoke-restmethod": "外部へ通信する命令があります",
    "send-mailmessage": "メールを送る命令があります",
    "stop-process": "プロセスを止める命令があります",
}


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
        # 警告は「実害のある命令」だけに絞る (2026-11-14、持ち主の
        # 判断で緩和)。許可リストに無いというだけでは警告にしない
        # (Set-Location のような日常の命令が要確認になっていた)。
        danger = _PS_DANGER_COMMANDS.get(real.lower())
        if danger is not None:
            warns.append((line, f"{danger}({real})"))
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


def phal_engine(path):
    """.phal ファイルの1行目の宣言を読む。"python" / "powershell" /
    None(宣言が無い・読めない)。

    宣言は `#!phal python` または `#!phal powershell`。BOM・前後の
    空白・大文字小文字は気にしない。読むのは1行目だけ。"""
    try:
        with open(path, "rb") as f:
            raw = f.readline()
    except OSError:
        return None
    line = raw.decode("utf-8-sig", errors="replace").strip().lower()
    if line == "#!phal python":
        return "python"
    if line == "#!phal powershell":
        return "powershell"
    return None


def check_script(path, config: dict) -> CheckResult:
    """拡張子で振り分けるチェックの入口。.phal は1行目の宣言で
    振り分ける。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".phal":
        engine = phal_engine(path)
        if engine == "powershell":
            return check_powershell_script(path)
        if engine == "python":
            return check_python_script(
                path, str(config.get("python") or "python"))
        result = CheckResult(sha256=file_sha256(path))
        result.status = STATUS_ERROR
        result.kind = "syntax"
        message = "1行目に #!phal python または #!phal powershell が必要です"
        result.issues = [CheckIssue(line=1, message=message)]
        result.jump_line = 1
        result.headline = message
        return result
    if suffix == ".ps1":
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
        self._job = None
        self.stopped = False

    def _attach(self, proc):
        self._proc = proc

    def _attach_job(self, job):
        self._job = job

    def _close_job(self):
        """ジョブを閉じる(冪等)。先に持ち手を外してから閉じるので、
        停止と時間切れ・後始末が重なっても二重には閉じない。"""
        job, self._job = self._job, None
        if job is not None:
            _job_close(job)

    def stop(self):
        self.stopped = True
        # 親が終わった後も孫がパイプを持っていると、親だけ止めても
        # 終わらない。ジョブも閉じて木ごと終わらせる(低1)。
        self._close_job()
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
    is_phal = path.suffix.lower() == ".phal"
    if is_phal:
        # .phal は1行目の宣言で言語を決める。宣言が無ければ Python
        # 扱いで試す(チェックでエラーになるので通常はここへ来ない)。
        is_ps = phal_engine(path) == "powershell"
    else:
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
            copy_name = BOM_COPY_PREFIX + path.name
            if is_phal:
                # PowerShell の -File は .ps1 でないと受けないため、
                # .phal の写しの末尾は .ps1 にする。
                copy_name = BOM_COPY_PREFIX + path.stem + ".ps1"
            bom_copy = path.parent / copy_name
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
        elif is_phal:
            # BOM の写しが要らない場合(BOM 付き・ASCII のみ)も、
            # 拡張子 .phal のままでは -File で受けないので、
            # .ps1 の写しで実行する。
            bom_copy = path.parent / (BOM_COPY_PREFIX + path.stem + ".ps1")
            try:
                bom_copy.write_bytes(path.read_bytes())
                run_path = bom_copy
            except OSError:
                bom_copy = None  # 写しが作れなければ元のまま試す
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
                handle._attach_job(job)
                job = None  # 閉じる操作は handle 側へ一本化する
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
                if handle is not None:
                    handle._close_job()
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
            if handle is not None:
                handle._close_job()
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
# コマンドID(ファイル名の先頭に付ける「セッション番号-通番」)
# ---------------------------------------------------------------
# AI 側がスクリプトを管理するための ID。ファイル名の先頭に
# 「12-03_check_db.py」のように付け、一覧の ID 列と実行結果の文に
# 出すことで、結果を貼り返したときにどのスクリプトの結果か分かる。
_COMMAND_ID_RE = re.compile(r"^(\d{1,3}-\d{1,3})_")


def parse_command_id(name: str):
    """ファイル名の先頭にあるコマンドIDを取り出す。無ければ None。

    形は「セッション番号-通番」のあとに "_"、続けて名前。
    例: "12-03_check_db.py" → "12-03"、"12-3_check_db.py" → "12-3"。
    表示はファイル名に書かれたまま(0 埋めは残す)。
    線引き:
      - 区切りは "_" のみ。"-" まで許すと "26-11-14" のような
        日付と区別がつかない。
      - 各部は3桁まで。"2026-11-14_notes.py" のような4桁の年で
        始まる日付を ID と取り違えないため。
    """
    match = _COMMAND_ID_RE.match(name)
    return match.group(1) if match else None


# 概要として見る先頭の行数。これより後ろのコメントは拾わない。
_SUMMARY_SCAN_LINES = 10


def extract_summary(path) -> str:
    """スクリプトの先頭コメント(何をする物か)を返す。無ければ ""。

    一覧の概要列に出すためのもの。読むのは先頭の数行だけ:
      - 空行、shebang (#!...)、.phal の宣言行 (#!phal ...) は飛ばす。
      - 最初に残った行が # で始まるコメントなら、# と前後の空白を
        外した中身を返す。コードや docstring が先に来たら ""。
      - コメントがファイル自身のコマンドID(「1-3:」「1-3 」)で
        始まっていたら、その接頭辞も外す(ID は ID 列に出ている)。
    文字コードは BOM を外し、読めない文字は置き換えて読み進める。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(8192)
    except OSError:
        return ""
    text = raw.decode("utf-8-sig", errors="replace")
    for line in text.splitlines()[:_SUMMARY_SCAN_LINES]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#!"):
            continue  # shebang と .phal の宣言行
        if not stripped.startswith("#"):
            return ""
        summary = stripped.lstrip("#").strip()
        command_id = parse_command_id(Path(path).name)
        if command_id and summary.startswith(command_id):
            tail = summary[len(command_id):]
            sep = re.match(r"\s*:\s*|\s+", tail)
            if sep:
                summary = tail[sep.end():]
        return summary
    return ""


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


def _split_output_lines(text: str) -> list:
    """出力を行に分ける(truncate_text と同じ数え方)。

    改行を \n にそろえ、末尾の改行を1つ外してから分ける。
    空の出力は 0 行になる。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        return []
    return text.split("\n")


def _collapse_repeated_lines(lines: list) -> list:
    """連続する同じ行を1行にまとめる。

    2回以上続いた行は「（同じ行が N 回）」を末尾に付けて1行に
    する。空行(空白だけの行)はまとめない(区切りが消えると
    読みにくくなるため)。"""
    collapsed = []
    run_line = None
    run_count = 0

    def flush():
        nonlocal run_line, run_count
        if run_line is None:
            return
        if run_count >= 2:
            collapsed.append(f"{run_line}（同じ行が {run_count} 回）")
        else:
            collapsed.append(run_line)
        run_line = None
        run_count = 0

    for line in lines:
        if line.strip() and line == run_line:
            run_count += 1
        else:
            flush()
            run_line = line
            run_count = 1
    flush()
    return collapsed


def _result_first_line(label: str, run: RunResult, config: dict,
                       total_lines: int = None) -> str:
    """結果文の1行目。total_lines があるときは末尾に「・全N行」を付ける
    (要約モードで、行数を先に知らせるため)。"""
    if run.timed_out:
        timeout = _int_or(config.get("timeout_seconds"), 120, 1, 3600)
        first = f"[実行結果] {label}  時間切れで止めました({timeout}秒)"
    elif run.stopped:
        first = f"[実行結果] {label}  停止しました"
    else:
        code = run.exit_code if run.exit_code is not None else "-"
        first = f"[実行結果] {label}  終了コード {code}  {run.seconds:.1f}秒"
    if run.start_error:
        first += f"  起動エラー: {run.start_error}"
    if total_lines is not None:
        first += f"・全{total_lines}行"
    return first


def _summary_stdout_section(out_lines: list) -> str:
    """要約文の出力の段。先頭15行と末尾10行だけ見せる。

    合計25行以下なら全部見せる。見出しの行数は stdout の行数。"""
    if not out_lines:
        body = "(なし)"
    elif len(out_lines) <= 25:
        body = "\n".join(_collapse_repeated_lines(out_lines))
    else:
        head = _collapse_repeated_lines(out_lines[:15])
        tail = _collapse_repeated_lines(out_lines[-10:])
        omitted = len(out_lines) - 25
        body = "\n".join(
            head + [f"…（中略：{omitted}行）…"] + tail)
    return f"--- 出力（要約・全{len(out_lines)}行） ---\n{body}"


def _summary_stderr_section(err_lines: list) -> str:
    """要約文のエラー出力の段。先頭10行だけ見せる。"""
    if not err_lines:
        body = "(なし)"
    else:
        shown = _collapse_repeated_lines(err_lines[:10])
        body = "\n".join(shown)
        if len(err_lines) > 10:
            body += f"\n…あと{len(err_lines) - 10}行"
    return f"--- エラー出力（先頭・全{len(err_lines)}行） ---\n{body}"


def save_full_output(run: RunResult, config: dict,
                     command_id: str = None) -> str | None:
    """実行結果の全文を data_dir() の下へ保存し、パスを返す。

    要約モードで、クリップボード文には要約だけを入れて全文は
    このファイルに置くためのもの。中身は build_result_text と
    同じ1行目に、出力とエラー出力の全文を続けたもの。LF・UTF-8
    で書く。保存に失敗したら None(呼ぶ側は従来の切り詰め文に
    落とす)。"""
    config = normalize_config(config)
    label = f"{command_id} {run.name}" if command_id else run.name
    out_lines = _split_output_lines(run.stdout)
    err_lines = _split_output_lines(run.stderr)
    total_lines = len(out_lines) + len(err_lines)
    first = _result_first_line(label, run, config, total_lines)
    body = (first + "\n--- 出力 ---\n" + run.stdout
            + "\n--- エラー出力 ---\n" + run.stderr)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    cid = command_id or "noid"
    filename = f"{cid}_{stamp}_{digest}.txt"
    try:
        folder = data_dir() / "phal-outputs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(body)
        return str(path)
    except OSError:
        return None


def build_result_text(name: str, run: RunResult, config: dict,
                      command_id: str = None) -> str:
    """実行したときにクリップボードへ入れる文。

    command_id(ファイル名先頭のコマンドID)があるときは、1行目を
    「[実行結果] 12-3 名前 …」にして、貼り返した側が対応付けられる
    ようにする。無いときの文は今までと完全に同じ。

    要約モード(summary_mode が真)で、出力とエラー出力の合計が
    summary_lines 行または summary_chars 文字を超えたら、全文は
    ファイルへ置いて要約だけを返す。短い出力は今までと完全に
    同じ文になる。全文の保存に失敗したら、従来の切り詰め文に
    落とす(要約を諦めても実行結果は必ず返す)。"""
    config = normalize_config(config)
    label = f"{command_id} {name}" if command_id else name
    out_lines = _split_output_lines(run.stdout)
    err_lines = _split_output_lines(run.stderr)
    total_lines = len(out_lines) + len(err_lines)
    total_chars = len(run.stdout) + len(run.stderr)
    if config["summary_mode"] and (
            total_lines > config["summary_lines"]
            or total_chars > config["summary_chars"]):
        full_path = save_full_output(run, config, command_id)
        if full_path is not None:
            first = _result_first_line(label, run, config, total_lines)
            parts = [first,
                     _summary_stdout_section(out_lines),
                     _summary_stderr_section(err_lines),
                     f"全文: {full_path}",
                     "「○行目付近を見せて」と頼めば取り出せます"]
            if run.policy_stop:
                parts.append(POLICY_NOTE)
            return "\n".join(parts)
    first = _result_first_line(label, run, config)
    parts = [first,
             _section("出力", run.stdout, config),
             _section("エラー出力", run.stderr, config)]
    if run.policy_stop:
        parts.append(POLICY_NOTE)
    return "\n".join(parts)


def build_return_text(name: str, reason: str, run: RunResult = None,
                      config: dict = None, script_text: str = "",
                      command_id: str = None) -> str:
    """「差し戻す」でクリップボードへ入れる文。

    事前チェックを通ったのに実行時にエラーになったスクリプトを、
    証拠(実行結果)と全文つきで AI へ返すためのもの。貼り返した側が
    同じ ID で直した版を出せるよう、1行目は結果文と同じ対応付け
    (ID + ファイル名)にする。実行結果の部分は build_result_text を
    そのまま使う(二重に整えない)。"""
    config = normalize_config(config or {})
    label = f"{command_id} {name}" if command_id else name
    parts = [f"[差し戻し] {label}"]
    reason = (reason or "").strip()
    if reason:
        parts.append(f"理由: {reason}")
    if run is not None:
        parts.append("--- 実行結果 ---")
        parts.append(build_result_text(name, run, config,
                                       command_id=command_id))
    parts.append("--- スクリプト ---")
    parts.append(script_text)
    return "\n".join(parts)


def move_to_recycle_bin(path) -> bool:
    """ファイルを Windows のゴミ箱へ移す。成功したら True。

    戻せる消し方が要るので、shell32 の SHFileOperationW に
    FOF_ALLOWUNDO を付けて頼む(確認ダイアログも出さない)。
    Windows 以外と失敗時は False(呼ぶ側が失敗を伝えること)。"""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class _SHFileOpStructW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    fo_delete = 3
    fof_allowundo = 0x0040
    fof_noconfirmation = 0x0010
    try:
        op = _SHFileOpStructW()
        op.wFunc = fo_delete
        # pFrom は二重の終端(パス + "\0\0")が要る。
        op.pFrom = str(Path(path)) + "\0\0"
        op.fFlags = fof_allowundo | fof_noconfirmation
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except (OSError, ValueError, AttributeError):
        return False
    return result == 0 and not op.fAnyOperationsAborted


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
