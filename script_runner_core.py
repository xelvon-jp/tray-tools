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
import locale
import os
import platform
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

# 実行ログ (append_run_log) のファイル名。動作ログ (1.89) の
# ACTIVITY_LOG_FILENAME ("script_runner_log.json") とは別物。
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
    "mini_mode": False,        # 小窓モード (上部だけ表示、2.27)
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
    config["mini_mode"] = bool(config["mini_mode"])
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


# DL の開き直し引き継ぎ (script_runner_dl_session.json)。
# パネルが閉じるときに、その時点で表示していた DL 候補の key と
# 閉じた時刻を残す。開き直した先では、閉じてから間もない場合に
# 限り「再起動の続き」とみなし、起動時の候補スナップショットを
# この key だけにする (1.70)。再起動の合間に届いた新しい候補を
# 「起動時の既存分」として自動 DL から外してしまう問題への対処で、
# 古い候補の落とし直し防止 (スナップショット自体) は、間が空いた
# 開き直しでは従来どおり効く。
DL_SESSION_FILENAME = "script_runner_dl_session.json"
DL_SESSION_CONTINUATION_SECONDS = 180


def dl_session_path() -> Path:
    return data_dir() / DL_SESSION_FILENAME


def save_dl_session(candidate_key, records_path=None) -> None:
    """閉じる時点の引き継ぎを書き出す。失敗しても黙って諦める
    (引き継ぎが無ければ通常のスナップショットに戻るだけ)。"""
    path = Path(records_path) if records_path is not None \
        else dl_session_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump({"candidate_key": candidate_key or "",
                       "closed_at": time.time()},
                      f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def load_dl_session(records_path=None) -> dict:
    """引き継ぎを読む。壊れている・読めない場合は空として扱う。"""
    path = Path(records_path) if records_path is not None \
        else dl_session_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def dl_session_continuation_key(session, now: float = None):
    """引き継ぎが「続き」扱いなら直前の候補の key を返す。

    閉じてからの経過が DL_SESSION_CONTINUATION_SECONDS 以内で、
    候補の key が残っている場合だけ。"""
    if not isinstance(session, dict):
        return None
    key = session.get("candidate_key")
    closed_at = session.get("closed_at")
    if not key or not isinstance(closed_at, (int, float)):
        return None
    if now is None:
        now = time.time()
    if now - closed_at > DL_SESSION_CONTINUATION_SECONDS:
        return None
    return key


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
# 前回の実行結果の控え (1.56)。
# 再実行の差分の相手。スクリプトのコマンドID (無ければファイル名)
# ごとに直近1件の結果文 (差分の対象になる部分) を残す。開き直し
# をまたぐ (phal_last_results.json)。実行済みハッシュの控えと
# 同じ作法 (同じ置き場・壊れたら空に倒す・別名で書いてから置き
# 換える)。結果テキストは各2万字で打ち切り、超えたらその旨を
# 控えに残す。件数は最大20件。
# ---------------------------------------------------------------
LAST_RESULTS_FILENAME = "phal_last_results.json"
LAST_RESULTS_LIMIT = 20
LAST_RESULT_MAX_CHARS = 20000


def last_results_path() -> Path:
    return data_dir() / LAST_RESULTS_FILENAME


def load_last_results(records_path=None) -> dict:
    """控え {id: {"text": str, "truncated": bool}} を読み込む。

    壊れている・読めない・形の違う項目は落とす (差分が出ない
    だけで、実行そのものは止めない)。"""
    path = Path(records_path) if records_path is not None \
        else last_results_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    records = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str):
            continue
        records[key] = {
            "text": text,
            "truncated": bool(entry.get("truncated", False)),
        }
    return records


def save_last_results(records: dict, records_path=None) -> None:
    """控えを書き出す。失敗しても黙って諦める (次回の再実行で
    差分が出ないだけ)。書きかけで壊さないよう、一度別名で書いて
    から置き換える。"""
    path = Path(records_path) if records_path is not None \
        else last_results_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(records, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def load_run_log_records(limit: int = 20, log_path=None) -> list:
    """実行ログ (script_runner_log.jsonl) の直近の記録を読む。

    append_run_log が1行1件で足す JSONL。壊れた行は読み飛ば
    す。戻り値は古い順で、最大 limit 件 (新しい方を残す)。
    一覧の合図 (1.92、素材の変更 1.94) の材料。"""
    path = Path(log_path) if log_path is not None \
        else data_dir() / LOG_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records[-limit:] if limit > 0 else records


def _history_line(record: dict) -> str:
    """実行ログの1件を一覧の1行にする (1.94)。

    形は「MM-DD HH:MM ファイル名 終了コード N（S秒）」で、
    止まった・時間切れは後ろに添える。"""
    stamp = ""
    iso = record.get("time")
    if isinstance(iso, str) and iso:
        try:
            stamp = datetime.fromisoformat(iso).strftime(
                "%m-%d %H:%M")
        except ValueError:
            stamp = ""
    name = str(record.get("file") or "(不明)")
    code = record.get("exit_code")
    code_text = f"終了コード {code}" if code is not None \
        else "終了コード不明"
    seconds = record.get("seconds")
    sec_text = f"（{seconds}秒）" if isinstance(
        seconds, (int, float)) else ""
    flags = ""
    if record.get("stopped"):
        flags += "・停止"
    if record.get("timed_out"):
        flags += "・時間切れ"
    head = f"{stamp} " if stamp else ""
    return f"{head}{name} {code_text}{sec_text}{flags}"


def build_history_text(records=None) -> str:
    """実行一覧の合図への文を組み立てる (1.92、素材は実行ログ
    へ変更 1.94)。

    当初は結果の控え (phal_last_results) の1行目を並べていた
    が、控えに残るのは見出しを除いた本文だったため「--- 出力
    ---」の行が並ぶ実地の不具合になった。実行ログ
    (append_run_log の JSONL) は時刻・ファイル名・終了コード
    を構造で持つので、こちらから作る。新しい順に最大20件。
    記録が無ければ、その旨の文を返す。"""
    if records is None:
        records = load_run_log_records(20)
    if not isinstance(records, list):
        records = []
    lines = [_history_line(r) for r in reversed(records)
             if isinstance(r, dict)]
    if not lines:
        return "実行の記録はまだありません。"
    return "\n".join(["[PHAL:実行一覧] 新しい順"] + lines)


def last_results_put(records: dict, key: str, text: str) -> None:
    """直近1件として控える。2万字を超えたらそこで打ち切り、
    truncated を立てる。20件を超えたら古いものから落とす
    (入れた順)。同じ id があれば新しい方で置き換える。"""
    if not key:
        return
    text = text or ""
    truncated = len(text) > LAST_RESULT_MAX_CHARS
    if truncated:
        text = text[:LAST_RESULT_MAX_CHARS]
    if key in records:
        del records[key]
    records[key] = {"text": text, "truncated": truncated}
    while len(records) > LAST_RESULTS_LIMIT:
        records.pop(next(iter(records)))


# ---------------------------------------------------------------
# チェックで止まった知らせの控え (1-53)。
# 自動実行の対象ファイルがチェックで実行不可になったとき、同じ
# 中身 (sha256) については知らせを1度だけ出すための控え。実行
# 済みハッシュの控えと同じ作法 (同じ置き場・期限と上限つき・
# 壊れたら空に倒す・別名で書いてから置き換える) で、ファイルは
# 分ける (「実行済み」と「チェックで止まった」は意味が違うので
# 混ぜない)。期限と上限の値は実行済みの控えとそろえる。
# ---------------------------------------------------------------
BLOCKED_NOTIFIED_FILENAME = "script_runner_blocked_notified.json"
BLOCKED_NOTIFIED_TTL_SECONDS = EXECUTED_HASH_TTL_SECONDS
BLOCKED_NOTIFIED_LIMIT = EXECUTED_HASH_LIMIT


def blocked_notified_path() -> Path:
    return data_dir() / BLOCKED_NOTIFIED_FILENAME


def load_blocked_notified(records_path=None) -> dict:
    """控え {sha256: 知らせた時刻} を読み込む。

    壊れている・読めない場合は空として扱う。期限 (7日) を超えた
    控えは、読み込みの時点で落とす。"""
    path = Path(records_path) if records_path is not None \
        else blocked_notified_path()
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
        if not isinstance(saved_at, (int, float)) \
                or now - saved_at > BLOCKED_NOTIFIED_TTL_SECONDS:
            continue
        records[sha] = saved_at
    return records


def save_blocked_notified(records: dict, records_path=None) -> None:
    """控えを書き出す。失敗しても黙って諦める (知らせがもう1度
    出るだけ)。書きかけで壊さないよう、一度別名で書いてから
    置き換える。"""
    path = Path(records_path) if records_path is not None \
        else blocked_notified_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(records, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def blocked_notified_put(records: dict, sha256: str) -> None:
    """知らせた中身の sha256 を控える。期限切れと上限超えは
    ここで落とす。"""
    if not sha256:
        return
    records[sha256] = time.time()
    now = time.time()
    for sha in [s for s, t in records.items()
                if not isinstance(t, (int, float))
                or now - t > BLOCKED_NOTIFIED_TTL_SECONDS]:
        del records[sha]
    while len(records) > BLOCKED_NOTIFIED_LIMIT:
        oldest = min(records, key=lambda s: records[s])
        del records[oldest]


def blocked_notified_contains(records: dict, sha256: str) -> bool:
    """その中身が知らせ済みの控えにあるか (期限内)。"""
    if not sha256:
        return False
    saved_at = records.get(sha256)
    return isinstance(saved_at, (int, float)) \
        and time.time() - saved_at <= BLOCKED_NOTIFIED_TTL_SECONDS


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


# ---------------------------------------------------------------
# 動作ログ (script_runner_log.json、1.89)。診断の控え (60件) とは
# 別に、開き直しをまたいで長く残すための記録。診断の束は直近を
# AI へ渡すためのもので、開き直しのたびの「チェック（控え
# から）」の列に押し流されてしまう。こちらは上限1000件で、
# 控えからのチェックは窓側が件数だけに畳んで入れる (個別の行
# は入れない)。ビューア (1.90) がこれを読む。
# ---------------------------------------------------------------
ACTIVITY_LOG_FILENAME = "script_runner_log.json"
LOG_LIMIT = 1000              # 動作ログに残す件数の上限


def log_path() -> Path:
    return data_dir() / ACTIVITY_LOG_FILENAME


# ファイルの出どころ (2.04)。ファイル名 → 届いたときに連携して
# いた AI (プロファイル名) の控え。一覧の「AI」列に出す。起動
# 前からあったファイルには付けない (どの AI からか分からない)。
ITEM_AI_FILENAME = "phal_item_ai.json"


def load_item_ai(path=None) -> dict:
    """ファイルの出どころの控え {ファイル名: AI名} を読む。"""
    target = Path(path) if path is not None \
        else data_dir() / ITEM_AI_FILENAME
    try:
        with open(target, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()
            if isinstance(v, str) and v}


def save_item_ai(mapping: dict, path=None) -> None:
    """ファイルの出どころの控えを書く。失敗しても黙る (表示用)。"""
    target = Path(path) if path is not None \
        else data_dir() / ITEM_AI_FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(mapping or {}, f, ensure_ascii=False)
        os.replace(tmp, target)
    except OSError:
        pass


def load_log_events(events_path=None) -> list:
    """動作ログ [{at, text}] を読み込む (古い順)。読みの作りは
    診断の控えと同じで、上限だけ違う。"""
    path = Path(events_path) if events_path is not None \
        else log_path()
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
        event = {"at": float(at), "text": text}
        ai = entry.get("ai")
        if isinstance(ai, str) and ai:
            event["ai"] = ai
        events.append(event)
    return events[-LOG_LIMIT:]


def save_log_events(events: list, events_path=None) -> None:
    path = Path(events_path) if events_path is not None \
        else log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(events[-LOG_LIMIT:], f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def log_append(events: list, text: str, at=None, ai=None) -> None:
    """動作ログへ1件足す。文の畳み方と長さの上限は診断と同じ。

    ai (2.04) は、その動きがどの AI との連携でのことか (有効な
    プロファイルの名前)。表示側 (format_log_lines) が [名前] の
    形で出す。無ければ付けない。"""
    text = " ".join(str(text).split())
    if not text:
        return
    if len(text) > DIAG_TEXT_LIMIT:
        text = text[:DIAG_TEXT_LIMIT - 1] + "…"
    event = {
        "at": float(at) if at is not None else time.time(),
        "text": text,
    }
    if isinstance(ai, str) and ai:
        event["ai"] = ai
    events.append(event)
    del events[:-LOG_LIMIT]


def format_log_lines(events, with_date: bool = False) -> list:
    """動作ログを「時刻 + 文」の行の並びにする (ビューア用)。

    ai が付いた記録 (2.04) は、時刻の後ろに [AI名] を出す。"""
    lines = []
    for e in events or []:
        at = e.get("at") or 0
        if with_date:
            stamp = time.strftime(
                "%m-%d %H:%M:%S", time.localtime(at))
        else:
            stamp = _diag_time_text(at)
        ai = e.get("ai")
        if isinstance(ai, str) and ai:
            lines.append(f"{stamp} [{ai}] {e.get('text', '')}")
        else:
            lines.append(f"{stamp} {e.get('text', '')}")
    return lines


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
        f"[PHAL:診断] Ver{version} {stamp}",
        f"自動化: DL={on_off('auto_dl')} 実行={on_off('auto_run')} "
        f"回答={on_off('auto_answer')} 送信={on_off('auto_send')} "
        f"診断={on_off('auto_diag')} 受領={on_off('auto_ack')} "
        f"リセット={on_off('auto_reset')} "
        f"再実行={on_off('auto_rerun')} "
        f"再送={on_off('auto_resend')} "
        f"設定={on_off('auto_settings')} "
        f"マニュアル={on_off('auto_manual')} "
        f"ファイル={on_off('auto_file')} "
        f"全文={on_off('auto_fulltext')} "
        f"一覧={on_off('auto_history')} "
        f"停止={on_off('auto_stop')} "
        f"ログ={on_off('auto_log')} "
        f"ツール={on_off('auto_tool')} "
        f"中継={on_off('auto_relay')}",
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
# 同じ発言で二度送らないための「消費済みの目印の ID」と、会話の
# 木のずれに強い「消費済みの第2の同定子 key」(1-49、目印を含む
# 直近40行のハッシュ。どちらか一致すれば消費済み)、短時間に繰り
# 返さないための「直近に送った時刻」を、開き直しをまたいで残す。
# 他の控えと同じ置き場・同じ作法 (壊れたら空に倒す)。
DIAG_AUTO_FILENAME = "script_runner_diag_auto.json"
DIAG_AUTO_COOLDOWN_SEC = 300.0  # 自動応答の間隔 (5分)


def diag_auto_path() -> Path:
    return data_dir() / DIAG_AUTO_FILENAME


def load_diag_auto(state_path=None) -> dict:
    """自動応答の控え {consumed_id, consumed_key, last_sent_at}
    を読み込む。

    古い控えに consumed_key が無ければ "" として扱う。壊れて
    いる・読めない・形が違う場合は、消費した物は無く送ったことも
    無い、として扱う (自動応答が理由でパネルが落ちてはなら
    ない)。"""
    path = Path(state_path) if state_path is not None \
        else diag_auto_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"consumed_id": "", "consumed_key": "",
                "last_sent_at": 0.0}
    if not isinstance(raw, dict):
        return {"consumed_id": "", "consumed_key": "",
                "last_sent_at": 0.0}
    consumed = raw.get("consumed_id")
    key = raw.get("consumed_key")
    sent_at = raw.get("last_sent_at")
    return {
        "consumed_id": consumed if isinstance(consumed, str) else "",
        "consumed_key": key if isinstance(key, str) else "",
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
        "consumed_key": str(state.get("consumed_key") or ""),
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
# リセットの合図の控え (1-52)。診断の自動応答 (上の diag_auto)
# と同じ作法だが、控えは混ぜずに別ファイルで持つ。
# AI の発言の中の目印の行 (copilot_loop.RESET_REQUEST_MARKER)
# を見つけたら、切替 auto_reset が入っているときだけ開き直す。
# 歯止めは、同じ発言で二度反応しないための「消費済みの目印の
# ID」と第2の同定子 key (どちらか一致すれば消費済み)、自動リ
# セット同士の間隔を最低10分空けるための「直近に開き直した
# 時刻」。他の控えと同じ置き場・同じ作法 (壊れたら空に倒す)。
# ---------------------------------------------------------------
RESET_AUTO_FILENAME = "script_runner_reset_auto.json"
RESET_AUTO_COOLDOWN_SEC = 600.0  # 自動リセット同士の最短間隔 (10分)


def reset_auto_path() -> Path:
    return data_dir() / RESET_AUTO_FILENAME


def load_reset_auto(state_path=None) -> dict:
    """リセットの控え {consumed_id, consumed_key, last_reset_at}
    を読み込む。

    古い控えに consumed_key が無ければ "" として扱う。壊れて
    いる・読めない・形が違う場合は、消費した物は無く開き直した
    ことも無い、として扱う (合図の判定が理由でパネルが落ちては
    ならない)。"""
    path = Path(state_path) if state_path is not None \
        else reset_auto_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"consumed_id": "", "consumed_key": "",
                "last_reset_at": 0.0,
                "last_payload": "", "last_answered_at": 0.0,
                "refire_notice_payload": "", "refire_notice_at": 0.0, "answered_map": {}}
    if not isinstance(raw, dict):
        return {"consumed_id": "", "consumed_key": "",
                "last_reset_at": 0.0,
                "last_payload": "", "last_answered_at": 0.0,
                "refire_notice_payload": "", "refire_notice_at": 0.0, "answered_map": {}}
    consumed = raw.get("consumed_id")
    key = raw.get("consumed_key")
    reset_at = raw.get("last_reset_at")
    payload = raw.get("last_payload")
    answered_at = raw.get("last_answered_at")
    return {
        "consumed_id": consumed if isinstance(consumed, str) else "",
        "consumed_key": key if isinstance(key, str) else "",
        "last_reset_at": float(reset_at)
        if isinstance(reset_at, (int, float)) else 0.0,
        "last_payload": payload if isinstance(payload, str) else "",
        "last_answered_at": float(answered_at)
        if isinstance(answered_at, (int, float)) else 0.0,
        "refire_notice_payload": raw.get("refire_notice_payload")
        if isinstance(raw.get("refire_notice_payload"), str)
        else "",
        "refire_notice_at": float(raw.get("refire_notice_at"))
        if isinstance(raw.get("refire_notice_at"), (int, float))
        else 0.0,
        "answered_map": _sanitize_answered_map(
            raw.get("answered_map"), raw),
    }


def save_reset_auto(state: dict, state_path=None) -> None:
    """リセットの控えを書き出す。失敗しても黙って諦める (次に
    開いたとき控えが少し古いだけ)。書きかけで壊さないよう、
    一度別名で書いてから置き換える。"""
    path = Path(state_path) if state_path is not None \
        else reset_auto_path()
    state = state if isinstance(state, dict) else {}
    payload = {
        "consumed_id": str(state.get("consumed_id") or ""),
        "consumed_key": str(state.get("consumed_key") or ""),
        "last_reset_at": float(state.get("last_reset_at") or 0.0),
        "last_payload": str(state.get("last_payload") or ""),
        "last_answered_at": float(
            state.get("last_answered_at") or 0.0),
        "refire_notice_payload": str(
            state.get("refire_notice_payload") or ""),
        "refire_notice_at": float(
            state.get("refire_notice_at") or 0.0),
        "answered_map": _sanitize_answered_map(
            state.get("answered_map")),
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
# 再実行の合図の控え (1.63)。診断・リセットと同じ作法で、控えは
# 混ぜずに別ファイルで持つ。
# AI の発言の中の目印の行 (copilot_loop.RERUN_REQUEST_MARKER +
# コマンドID) を見つけたら、切替 auto_rerun が入っているとき
# だけ、その ID のスクリプトを再実行する。歯止めは、同じ発言
# で二度反応しないための「消費済みの目印の ID」と第2の同定子
# key (どちらか一致すれば消費済み)、自動再実行同士の間隔を
# 最低5分空けるための「直近に再実行した時刻」。
# ---------------------------------------------------------------
RERUN_AUTO_FILENAME = "script_runner_rerun_auto.json"
RERUN_AUTO_COOLDOWN_SEC = 300.0  # 自動再実行同士の最短間隔 (5分)


def rerun_auto_path() -> Path:
    return data_dir() / RERUN_AUTO_FILENAME


def load_rerun_auto(state_path=None) -> dict:
    """再実行の控え {consumed_id, consumed_key, last_rerun_at}
    を読み込む。

    古い控えに consumed_key が無ければ "" として扱う。壊れて
    いる・読めない・形が違う場合は、消費した物は無く再実行
    したことも無い、として扱う (合図の判定が理由でパネルが
    落ちてはならない)。"""
    path = Path(state_path) if state_path is not None \
        else rerun_auto_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"consumed_id": "", "consumed_key": "",
                "last_rerun_at": 0.0,
                "last_payload": "", "last_answered_at": 0.0,
                "refire_notice_payload": "", "refire_notice_at": 0.0, "answered_map": {}}
    if not isinstance(raw, dict):
        return {"consumed_id": "", "consumed_key": "",
                "last_rerun_at": 0.0,
                "last_payload": "", "last_answered_at": 0.0,
                "refire_notice_payload": "", "refire_notice_at": 0.0, "answered_map": {}}
    consumed = raw.get("consumed_id")
    key = raw.get("consumed_key")
    rerun_at = raw.get("last_rerun_at")
    payload = raw.get("last_payload")
    answered_at = raw.get("last_answered_at")
    return {
        "consumed_id": consumed if isinstance(consumed, str) else "",
        "consumed_key": key if isinstance(key, str) else "",
        "last_rerun_at": float(rerun_at)
        if isinstance(rerun_at, (int, float)) else 0.0,
        "last_payload": payload if isinstance(payload, str) else "",
        "last_answered_at": float(answered_at)
        if isinstance(answered_at, (int, float)) else 0.0,
        "refire_notice_payload": raw.get("refire_notice_payload")
        if isinstance(raw.get("refire_notice_payload"), str)
        else "",
        "refire_notice_at": float(raw.get("refire_notice_at"))
        if isinstance(raw.get("refire_notice_at"), (int, float))
        else 0.0,
        "answered_map": _sanitize_answered_map(
            raw.get("answered_map"), raw),
    }


def save_rerun_auto(state: dict, state_path=None) -> None:
    """再実行の控えを書き出す。失敗しても黙って諦める (次に
    開いたとき控えが少し古いだけ)。書きかけで壊さないよう、
    一度別名で書いてから置き換える。"""
    path = Path(state_path) if state_path is not None \
        else rerun_auto_path()
    state = state if isinstance(state, dict) else {}
    payload = {
        "consumed_id": str(state.get("consumed_id") or ""),
        "consumed_key": str(state.get("consumed_key") or ""),
        "last_rerun_at": float(state.get("last_rerun_at") or 0.0),
        "last_payload": str(state.get("last_payload") or ""),
        "last_answered_at": float(
            state.get("last_answered_at") or 0.0),
        "refire_notice_payload": str(
            state.get("refire_notice_payload") or ""),
        "refire_notice_at": float(
            state.get("refire_notice_at") or 0.0),
        "answered_map": _sanitize_answered_map(
            state.get("answered_map")),
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
# 結果再送の合図の控え (1.75)
#
# AI の発言の中の目印の行 (copilot_loop.RESEND_REQUEST_MARKER +
# コマンドID) を見つけたら、切替 auto_resend が入っているとき
# だけ、その ID の直近の結果 (last_results の控え) を投稿し直
# す。歯止めは再実行と同じ作法: 同じ発言で二度反応しないため
# の消費済みの同定子 (id と key) と、再送同士の間隔 (最短60秒)。
# ---------------------------------------------------------------
RESEND_AUTO_FILENAME = "script_runner_resend_auto.json"
RESEND_AUTO_COOLDOWN_SEC = 60.0  # 自動再送同士の最短間隔 (60秒)


def resend_auto_path() -> Path:
    return data_dir() / RESEND_AUTO_FILENAME


def load_resend_auto(state_path=None) -> dict:
    """結果再送の控え {consumed_id, consumed_key, last_sent_at}
    を読み込む。壊れている場合は「消費した物は無く、送った
    ことも無い」として扱う (再実行の控えと同じ倒し方)。"""
    path = Path(state_path) if state_path is not None \
        else resend_auto_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"consumed_id": "", "consumed_key": "",
                "last_sent_at": 0.0,
                "last_payload": "", "last_answered_at": 0.0,
                "refire_notice_payload": "", "refire_notice_at": 0.0, "answered_map": {}}
    if not isinstance(raw, dict):
        return {"consumed_id": "", "consumed_key": "",
                "last_sent_at": 0.0,
                "last_payload": "", "last_answered_at": 0.0,
                "refire_notice_payload": "", "refire_notice_at": 0.0, "answered_map": {}}
    consumed = raw.get("consumed_id")
    key = raw.get("consumed_key")
    sent_at = raw.get("last_sent_at")
    payload = raw.get("last_payload")
    answered_at = raw.get("last_answered_at")
    return {
        "consumed_id": consumed if isinstance(consumed, str) else "",
        "consumed_key": key if isinstance(key, str) else "",
        "last_sent_at": float(sent_at)
        if isinstance(sent_at, (int, float)) else 0.0,
        "last_payload": payload if isinstance(payload, str) else "",
        "last_answered_at": float(answered_at)
        if isinstance(answered_at, (int, float)) else 0.0,
        "refire_notice_payload": raw.get("refire_notice_payload")
        if isinstance(raw.get("refire_notice_payload"), str)
        else "",
        "refire_notice_at": float(raw.get("refire_notice_at"))
        if isinstance(raw.get("refire_notice_at"), (int, float))
        else 0.0,
        "answered_map": _sanitize_answered_map(
            raw.get("answered_map"), raw),
    }


def save_resend_auto(state: dict, state_path=None) -> None:
    """結果再送の控えを書き出す (一時ファイルから置き換え)。"""
    path = Path(state_path) if state_path is not None \
        else resend_auto_path()
    state = state if isinstance(state, dict) else {}
    payload = {
        "consumed_id": str(state.get("consumed_id") or ""),
        "consumed_key": str(state.get("consumed_key") or ""),
        "last_sent_at": float(state.get("last_sent_at") or 0.0),
        "last_payload": str(state.get("last_payload") or ""),
        "last_answered_at": float(
            state.get("last_answered_at") or 0.0),
        "refire_notice_payload": str(
            state.get("refire_notice_payload") or ""),
        "refire_notice_at": float(
            state.get("refire_notice_at") or 0.0),
        "answered_map": _sanitize_answered_map(
            state.get("answered_map")),
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
# 設定確認・マニュアル再送の控え (1.85)。作りは結果再送の控えと
# 同じで、ファイルだけ分ける (混ぜない)。
# ---------------------------------------------------------------
SETTINGS_AUTO_FILENAME = "script_runner_settings_auto.json"
MANUAL_AUTO_FILENAME = "script_runner_manual_auto.json"
RELAY_AUTO_FILENAME = "script_runner_relay_auto.json"
SETTINGS_AUTO_COOLDOWN_SEC = 60.0  # 設定の自動応答同士の最短間隔
MANUAL_AUTO_COOLDOWN_SEC = 60.0    # マニュアル再送同士の最短間隔
RELAY_AUTO_COOLDOWN_SEC = 60.0     # 中継同士の最短間隔
RELAY_PENDING_TTL_SEC = 600.0      # 中継の保留控えの寿命 (2.13)
RELAY_RETRY_DELAY_SEC = 20.0       # 中継の送り直しまでの待ち (2.13)
RELAY_RETRY_MAX = 1                # 中継の送り直しの回数上限 (2.13)
RELAY_ACK_DELAY_SEC = 30.0         # 中継の成功ACKを保留する秒数 (2.27)


def _sanitize_answered_map(raw_map, raw=None) -> dict:
    """応答済みの履歴 (payload -> 応答時刻) を検証して返す
    (2.34)。形の違う要素は落とし、直近16件までにする。
    履歴が無い古い控えは last_payload/last_answered_at から
    種を作る (移行)。"""
    result = {}
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            if isinstance(k, str) and isinstance(v, (int, float)) \
                    and v > 0:
                result[k] = float(v)
    if not result and isinstance(raw, dict):
        payload = raw.get("last_payload")
        at = raw.get("last_answered_at")
        if isinstance(payload, str) and isinstance(at, (int, float)) \
                and at > 0:
            result[payload] = float(at)
    if len(result) > 16:
        keep = sorted(result.items(), key=lambda kv: kv[1])[-16:]
        result = dict(keep)
    return result


def _load_marker_auto(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {"consumed_id": "", "consumed_key": "",
                "last_sent_at": 0.0,
                "last_payload": "", "last_answered_at": 0.0,
                "refire_notice_payload": "", "refire_notice_at": 0.0, "answered_map": {}}
    sent_at = raw.get("last_sent_at")
    answered_at = raw.get("last_answered_at")
    notice_at = raw.get("refire_notice_at")
    return {
        "consumed_id": raw.get("consumed_id")
        if isinstance(raw.get("consumed_id"), str) else "",
        "consumed_key": raw.get("consumed_key")
        if isinstance(raw.get("consumed_key"), str) else "",
        "last_sent_at": float(sent_at)
        if isinstance(sent_at, (int, float)) else 0.0,
        "last_payload": raw.get("last_payload")
        if isinstance(raw.get("last_payload"), str) else "",
        "last_answered_at": float(answered_at)
        if isinstance(answered_at, (int, float)) else 0.0,
        # 応答済みの知らせを送った控え (2.00)。
        "refire_notice_payload": raw.get("refire_notice_payload")
        if isinstance(raw.get("refire_notice_payload"), str)
        else "",
        "refire_notice_at": float(notice_at)
        if isinstance(notice_at, (int, float)) else 0.0,
        # 応答済みの履歴 (2.34)。payload -> 応答時刻。単一の
        # last_payload だけでは、応答済みの目印が複数残ると
        # 交互に再検出されて抑止をすり抜けた (2026-11-16
        # 実地) ため、直近分はまとめて持つ。
        "answered_map": _sanitize_answered_map(
            raw.get("answered_map"), raw),
    }


def _save_marker_auto(state, path, extra=None) -> None:
    state = state if isinstance(state, dict) else {}
    payload = {
        "consumed_id": str(state.get("consumed_id") or ""),
        "consumed_key": str(state.get("consumed_key") or ""),
        "last_sent_at": float(state.get("last_sent_at") or 0.0),
        "last_payload": str(state.get("last_payload") or ""),
        "last_answered_at": float(
            state.get("last_answered_at") or 0.0),
        "refire_notice_payload": str(
            state.get("refire_notice_payload") or ""),
        "refire_notice_at": float(
            state.get("refire_notice_at") or 0.0),
        "answered_map": _sanitize_answered_map(
            state.get("answered_map")),
    }
    if isinstance(extra, dict):
        payload.update(extra)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def load_settings_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / SETTINGS_AUTO_FILENAME
    return _load_marker_auto(path)


def save_settings_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / SETTINGS_AUTO_FILENAME
    _save_marker_auto(state, path)


def load_manual_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / MANUAL_AUTO_FILENAME
    return _load_marker_auto(path)


def save_manual_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / MANUAL_AUTO_FILENAME
    _save_marker_auto(state, path)


def sanitize_relay_pending(raw):
    """中継の保留控え (2.13) を検証して返す。

    控えは {id, key, body, at, retries, not_before, src, dst}。
    形が違う・本文が空なら None (保留なしとして扱う)。"""
    if not isinstance(raw, dict):
        return None
    mid = raw.get("id")
    body = raw.get("body")
    if not isinstance(mid, str) or not mid:
        return None
    if not isinstance(body, str) or not body:
        return None
    key = raw.get("key")
    at = raw.get("at")
    retries = raw.get("retries")
    not_before = raw.get("not_before")
    src_name = raw.get("src")
    dst_name = raw.get("dst")
    return {
        "id": mid,
        "key": key if isinstance(key, str) else "",
        "body": body,
        "at": float(at) if isinstance(at, (int, float)) else 0.0,
        "retries": int(retries)
        if isinstance(retries, int) else 0,
        "not_before": float(not_before)
        if isinstance(not_before, (int, float)) else 0.0,
        # 中継の経路 (2.17)。2.13 までの控えには無く、空のまま
        # (窓側が従来の主→副として扱う)。
        "src": src_name if isinstance(src_name, str) else "",
        "dst": dst_name if isinstance(dst_name, str) else "",
    }


def load_relay_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / RELAY_AUTO_FILENAME
    state = _load_marker_auto(path)
    # 保留控えは中継専用の欄なので、共有の読み書きとは別に
    # 生の JSON から拾う (2.13)。
    pending = None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        pending = sanitize_relay_pending(raw.get("pending"))
    state["pending"] = pending
    return state


def save_relay_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / RELAY_AUTO_FILENAME
    state = state if isinstance(state, dict) else {}
    pending = sanitize_relay_pending(state.get("pending"))
    _save_marker_auto(state, path, extra={"pending": pending})


# 設定確認の報告に出す自動化の短い名前 (診断の束と同じ並び)。
_SETTINGS_AUTO_LABELS = (
    ("auto_dl", "DL"), ("auto_run", "実行"),
    ("auto_answer", "回答"), ("auto_send", "送信"),
    ("auto_diag", "診断"), ("auto_ack", "受領"),
    ("auto_reset", "リセット"), ("auto_rerun", "再実行"),
    ("auto_resend", "再送"), ("auto_settings", "設定"),
    ("auto_manual", "マニュアル"), ("auto_file", "ファイル"),
    ("auto_fulltext", "全文"),
    ("auto_history", "一覧"),
    ("auto_stop", "停止"),
    ("auto_log", "ログ"),
    ("auto_tool", "ツール"),
    ("auto_relay", "中継"),
)


def build_settings_report(section, version) -> str:
    """設定確認の合図への報告を組み立てる (1.85)。

    script_runner 節 (設定ファイルの中身) から、AI がループを
    回すのに要る値を平文で並べる。窓の見た目の細部や、投げ先の
    窓一致の欄そのものは出さない (長いし、要らない)。"""
    section = section if isinstance(section, dict) else {}

    def on_off(value):
        return "入" if value else "切"

    lines = [f"[PHAL:設定] Ver{version}"]
    # 監視フォルダは、設定が空なら実際に見張っている既定の場所
    # (ダウンロードフォルダ) を出す (生の空文字は意味がない)。
    watch = section.get("watch_folder")
    if isinstance(watch, str) and watch.strip():
        lines.append(f"- 監視フォルダ: {watch}")
    else:
        lines.append(
            f"- 監視フォルダ: {resolve_watch_folder(section)}"
            "（既定のダウンロードフォルダ）")
    work = section.get("work_folder")
    lines.append(
        f"- 作業フォルダ: {work if isinstance(work, str) and work else '(未選択)'}")
    lines.append(f"- ダークモード: {on_off(section.get('dark_mode'))}")
    if section.get("summary_mode", True):
        lines.append(
            f"- 要約モード: 入（{section.get('summary_lines', 100)}行"
            f"・{section.get('summary_chars', 8000)}字を超えたら要約）")
    else:
        lines.append("- 要約モード: 切")
    timeout = section.get("timeout_seconds")
    lines.append(
        f"- 実行の上限: {timeout}秒"
        if isinstance(timeout, (int, float)) else "- 実行の上限: (不明)")
    saved_intro = section.get("intro_prompt")
    if isinstance(saved_intro, str) and saved_intro.strip():
        lines.append(
            f"- 開始プロンプト: 保存した文を使う（{len(saved_intro)}字）")
    else:
        lines.append("- 開始プロンプト: 自動生成")
    auto_text = " ".join(
        f"{label}={on_off(section.get(key))}"
        for key, label in _SETTINGS_AUTO_LABELS)
    lines.append(f"自動化: {auto_text}")
    watch = section.get("ai_watch_profiles")
    if isinstance(watch, list):
        watch_names = [value for value in watch
                       if isinstance(value, str) and value]
        lines.append(
            "- 見張る窓: "
            + ("、".join(watch_names) if watch_names else "なし")
            + "（2.20 の集合形式）")
    elif section.get("ai_dual_watch") is True:
        secondary = section.get("ai_secondary_profile")
        lines.append(
            "- 二重待受: 入（副: "
            + (secondary if isinstance(secondary, str) and secondary
               else "自動で選ぶ") + "）")
    else:
        lines.append("- 二重待受: 切")
    tools = sanitize_cli_tools(section.get("cli_tools"))
    tool_paths = sanitize_cli_tool_paths(section.get("cli_tool_paths"))
    lines.append(
        "- ツール実行の許可リスト: "
        + (", ".join(tools) if tools else "(なし)"))
    # 登録 (名前→パス) と解決の結果と使い方 (2.00)。解決は
    # 実在を見るので、この報告を出した PC 上での答えになる。
    tool_usages = sanitize_cli_tool_usages(
        section.get("cli_tool_usages"))
    folder = tools_folder()
    placed = []
    if folder.is_dir():
        placed = sorted(
            p.name for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() in TOOL_FOLDER_EXTS)
    placed_names = {normalize_tool_name(n) for n in placed}
    for name in sorted(
            set(tools) | set(tool_paths) | placed_names):
        found, why = resolve_cli_tool(
            name, tools, tool_paths, [folder])
        if name in tool_paths:
            line = (
                f"- ツール {name}: 登録 {tool_paths[name]}"
                + ("" if found else "（見つかりません）"))
        elif found:
            line = f"- ツール {name}: ツール置き場 {found}"
        elif why == "no_path":
            line = f"- ツール {name}: （パス未設定）"
        else:
            line = f"- ツール {name}: （見つかりません）"
        if name in tool_usages:
            line += f"（使い方: {tool_usages[name]}）"
        lines.append(line)
    lines.append(
        f"- ツール置き場: {folder}"
        + (f"（中身: {', '.join(placed)}）" if placed
           else "（空）"))
    profiles = section.get("ai_profiles")
    active = section.get("ai_profile_active")
    if isinstance(profiles, list) and profiles:
        lines.append("AIプロファイル:")
        for i, entry in enumerate(profiles):
            if not isinstance(entry, dict):
                continue
            target = entry.get("target") \
                if isinstance(entry.get("target"), dict) else {}
            mark = "（有効）" if i == active else ""
            label = target.get("label") or ""
            method = target.get("attach_method") or "auto"
            lines.append(
                f"- {entry.get('name') or ''}{mark}: ラベル={label}"
                f" / 添付方式={method}"
                f" / ZIP名前変え={on_off(target.get('zip_as_txt') is True)}")
    else:
        lines.append("AIプロファイル: (未設定)")
    return "\n".join(lines)


# ---------------------------------------------------------------
# ファイルリクエストの控えとパスの解決 (1.88)。控えの作りは
# 設定・マニュアルと同じ (汎用の _load/_save_marker_auto)。
# ---------------------------------------------------------------
FILE_AUTO_FILENAME = "script_runner_file_auto.json"
FILE_AUTO_COOLDOWN_SEC = 60.0  # ファイル送信同士の最短間隔


def load_file_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / FILE_AUTO_FILENAME
    return _load_marker_auto(path)


def save_file_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / FILE_AUTO_FILENAME
    _save_marker_auto(state, path)


# ---------------------------------------------------------------
# 全文リクエストの控えと全文ファイル探し (1.91)。控えの作りは
# ファイルリクエストと同じ (汎用の _load/_save_marker_auto)。
# ---------------------------------------------------------------
FULLTEXT_AUTO_FILENAME = "script_runner_fulltext_auto.json"
FULLTEXT_AUTO_COOLDOWN_SEC = 60.0  # 全文送信同士の最短間隔


def load_fulltext_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / FULLTEXT_AUTO_FILENAME
    return _load_marker_auto(path)


def save_fulltext_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / FULLTEXT_AUTO_FILENAME
    _save_marker_auto(state, path)


# ---------------------------------------------------------------
# 実行一覧リクエストの控え (1.92)。控えの作りは全文リクエストと
# 同じ (汎用の _load/_save_marker_auto)。
# ---------------------------------------------------------------
HISTORY_AUTO_FILENAME = "script_runner_history_auto.json"
HISTORY_AUTO_COOLDOWN_SEC = 60.0  # 一覧の自動応答同士の最短間隔


def load_history_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / HISTORY_AUTO_FILENAME
    return _load_marker_auto(path)


def save_history_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / HISTORY_AUTO_FILENAME
    _save_marker_auto(state, path)


# ---------------------------------------------------------------
# 停止リクエストの控え (1.93)。控えの作りは実行一覧と同じ
# (汎用の _load/_save_marker_auto)。
# ---------------------------------------------------------------
STOP_AUTO_FILENAME = "script_runner_stop_auto.json"
STOP_AUTO_COOLDOWN_SEC = 60.0  # 停止の自動応答同士の最短間隔

# 合図の再応答の抑止 (1.95)。応答済みの合図が、開き直しをまた
# いで同定子 (id・key) が外れた状態で再検出されることがある
# ため、種類ごとの控えに「最後に応答した中身 (last_payload)」
# と「応答した時刻 (last_answered_at)」を持ち、同じ中身への
# 応答はこの秒数の間は繰り返さない。
MARKER_REFIRE_SUPPRESS_SEC = 900.0  # 再検出を黙って見送る間隔 (15分)。これを過ぎた再検出は実行せず「応答済み」の知らせを1回だけ送る (2.00)


def load_stop_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / STOP_AUTO_FILENAME
    return _load_marker_auto(path)


def save_stop_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / STOP_AUTO_FILENAME
    _save_marker_auto(state, path)


# ---------------------------------------------------------------
# 動作ログリクエストの控えと、指定の解釈・選び出し (1.96)。
# 控えの作りは停止リクエストと同じ (汎用の _load/_save_marker
# _auto)。load_log_events (動作ログ本体の読み) とは別物なので
# 混同しないこと。
# ---------------------------------------------------------------
LOG_AUTO_FILENAME = "script_runner_log_auto.json"
LOG_AUTO_COOLDOWN_SEC = 60.0  # ログの自動応答同士の最短間隔

LOG_REQUEST_DEFAULT_COUNT = 50   # 指定なしのときの件数
LOG_REQUEST_MAX_COUNT = 500      # 件数指定の上限

_LOG_DATE_TOKEN_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_LOG_RANGE_TOKEN_RE = re.compile(
    r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")
_LOG_COUNT_TOKEN_RE = re.compile(r"^\d+$")


def load_log_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / LOG_AUTO_FILENAME
    return _load_marker_auto(path)


def save_log_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / LOG_AUTO_FILENAME
    _save_marker_auto(state, path)


# ---------------------------------------------------------------
# ツール実行リクエストの控えと、引数文字列の分割・許可リスト・
# 実行 (1.98)。引数の分割 (split_command_args) は再実行の引数
# (1.97) でも使う。控えの作りは他の合図と同じ (汎用の
# _load/_save_marker_auto)。
# ---------------------------------------------------------------
TOOL_AUTO_FILENAME = "script_runner_tool_auto.json"
TOOL_AUTO_COOLDOWN_SEC = 60.0  # ツール実行の応答同士の最短間隔

# 許可リストの既定 (設定 cli_tools が無いとき)。Wireshark の
# コマンドライン一式と、Windows 標準の ipconfig。
DEFAULT_CLI_TOOLS = (
    "tshark", "capinfos", "editcap", "mergecap", "dumpcap",
    "ipconfig",
)


def load_tool_auto(state_path=None) -> dict:
    path = Path(state_path) if state_path is not None \
        else data_dir() / TOOL_AUTO_FILENAME
    return _load_marker_auto(path)


def save_tool_auto(state: dict, state_path=None) -> None:
    path = Path(state_path) if state_path is not None \
        else data_dir() / TOOL_AUTO_FILENAME
    _save_marker_auto(state, path)


def split_command_args(text) -> list:
    """引数文字列を引数の並びに分ける (1.97 / 1.98)。

    Windows のパス (バックスラッシュ) を壊さないよう、引用符を
    残す形 (posix=False) で分けてから、各語の前後の引用符を
    外す。引用符で囲まれた空白は1語にまとまる。引用符が閉じて
    いないときは ValueError (呼ぶ側が理由として返す)。"""
    import shlex

    args = []
    for token in shlex.split(str(text or ""), posix=False):
        if len(token) >= 2 and token[0] == token[-1] \
                and token[0] in "\"'":
            token = token[1:-1]
        args.append(token)
    return args


def normalize_tool_name(name) -> str:
    """ツール名を比べられる形に整える (小文字・拡張子なし・
    パスが付いていれば末尾の名前だけ)。"""
    base = str(name or "").strip().replace("/", "\\")
    base = base.rsplit("\\", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.lower()


def sanitize_cli_tools(value) -> list:
    """設定の許可リスト (cli_tools) を整える。

    文字列の並びだけを残し、整えた名前の重複を除く。値が並び
    で無ければ既定 (DEFAULT_CLI_TOOLS) を返す。空の並びは
    「何も許可しない」として尊重する。"""
    if not isinstance(value, list):
        return list(DEFAULT_CLI_TOOLS)
    result = []
    for item in value:
        name = normalize_tool_name(item)
        if name and name not in result:
            result.append(name)
    return result


def sanitize_cli_tool_paths(value) -> dict:
    """設定のツール登録 (cli_tool_paths: 名前→パス) を整える
    (2.00)。

    辞書でなければ空。名前は normalize_tool_name で整え、
    パスは空でない文字列だけを残す。"""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, raw in value.items():
        name = normalize_tool_name(key)
        path = str(raw or "").strip()
        if name and path:
            result[name] = path
    return result


def sanitize_cli_tool_usages(value) -> dict:
    """設定のツールの使い方 (cli_tool_usages: 名前→説明) を
    整える (2.00)。

    使い方の文は、設定の報告に載って AI へ届く。辞書で
    無ければ空。名前は整え、文は前後の空白を落として
    空でないものだけを残す。"""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, raw in value.items():
        name = normalize_tool_name(key)
        text = str(raw or "").strip()
        if name and text:
            result[name] = text
    return result


# ツール置き場 (2.00): ここに入れた実行ファイル (.exe/.bat/
# .cmd/.py) は、許可リストに無くても名前で実行できる。置く
# 行為そのものが許可になる、自作ツール向けの置き場。
TOOL_FOLDER_NAME = "phal_tools"
TOOL_FOLDER_EXTS = (".exe", ".bat", ".cmd", ".py")


def tools_folder() -> Path:
    """ツール置き場のパス (data_dir の下)。"""
    return data_dir() / TOOL_FOLDER_NAME


def tool_command(exe_path) -> list:
    """解決したツールの実行コマンドを作る (2.00)。

    .py のツールは Python で実行する (PHAL 自身が動いている
    インタプリタ。pythonw で動いているときは、同じ場所の
    python.exe を使う)。それ以外はそのまま。"""
    path = Path(exe_path)
    if path.suffix.lower() != ".py":
        return [str(path)]
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        sibling = python.with_name("python.exe")
        if sibling.is_file():
            python = sibling
    return [str(python), str(path)]


def search_cli_tool(name, search_dirs=None):
    """ツールを PATH と Wireshark の既定の置き場から探す
    (2.01)。

    「ツール設定」の「探す」ボタン用。実行時の解決
    (resolve_cli_tool) は登録されたパスが正で、ここは使わ
    ない (探すのは設定のときだけ)。search_dirs は Wireshark
    フォルダの差し替え (テスト用)。"""
    tool = normalize_tool_name(name)
    if not tool:
        return None
    found = shutil.which(tool) or shutil.which(tool + ".exe")
    if found:
        return Path(found)
    dirs = list(search_dirs or [])
    if not dirs:
        for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_key)
            if base:
                dirs.append(str(Path(base) / "Wireshark"))
    for folder in dirs:
        candidate = Path(folder) / (tool + ".exe")
        if candidate.is_file():
            return candidate
    return None


def resolve_cli_tool(name, allowed, paths=None, extra_dirs=None):
    """ツール名を、許可と実在で解決する (1.98、2.00 で拡張、
    2.01 で登録パスを正にした)。

    戻り値は (Path, "") か (None, 理由)。理由は "not_allowed"
    (許可が無い)、"no_path" (許可はあるがパスが未設定)、
    "not_found" (登録されたパスに実在しない)。
    許可になるのは: 許可リスト (allowed) にある、登録
    (paths: 名前→パス) がある、ツール置き場 (extra_dirs)
    に実行ファイルがある、のどれか。
    実行時は登録されたパスが正で、PATH などは探しに行かな
    い (2.01、ユーザーの指定)。パスが空なら "no_path" で
    エラーにし、探して埋めるのは設定画面の「探す」の役割。
    登録のパスがあればそれだけを見る (別の実体を黙って
    実行しない)。ツール置き場の実在は従来どおり許可になる。"""
    tool = normalize_tool_name(name)
    if not tool:
        return None, "not_allowed"
    registered = sanitize_cli_tool_paths(paths)
    if tool in registered:
        candidate = Path(registered[tool])
        if candidate.is_file():
            return candidate, ""
        return None, "not_found"
    allowed_names = {normalize_tool_name(a) for a in (allowed or [])}
    in_dirs = None
    for folder in extra_dirs or []:
        for ext in TOOL_FOLDER_EXTS:
            candidate = Path(folder) / (tool + ext)
            if candidate.is_file():
                in_dirs = candidate
                break
        if in_dirs is not None:
            break
    if tool not in allowed_names and in_dirs is None:
        return None, "not_allowed"
    if in_dirs is not None:
        return in_dirs, ""
    return None, "no_path"


def tool_not_found_note(name, paths=None, extra_dirs=None) -> str:
    """ツールが見つからなかった返事に付ける、探した場所の
    説明 (2.00、2.01 で実行時の探し方に合わせて更新)。"""
    tool = normalize_tool_name(name)
    registered = sanitize_cli_tool_paths(paths)
    places = []
    if tool in registered:
        places.append(f"登録されたパス {registered[tool]}")
    for folder in extra_dirs or []:
        places.append(f"ツール置き場 {folder}")
    return "（探した場所: " + " / ".join(places) + "）"


def parse_log_request(params, now=None) -> dict:
    """ログリクエストの指定を解釈する (1.96)。

    params は目印の後ろの文字列 (空白区切り、順不同)。数字だけ
    の語は件数、「YYYY-MM-DD」は日付、「HH:MM-HH:MM」は時刻
    範囲として読む。認識できない語は無視する。戻り値は
    {"date": date|None, "start_min": int|None,
    "end_min": int|None, "count": int, "label": str}。
    label は適用した範囲の表示 (「直近50件」／「2026-11-15」／
    「16:00〜16:30」／日付と範囲の合成)。件数は
    1〜LOG_REQUEST_MAX_COUNT に丸める。"""
    now = now if now is not None else datetime.now()
    date = None
    start_min = None
    end_min = None
    count = LOG_REQUEST_DEFAULT_COUNT
    for token in str(params or "").split():
        m = _LOG_DATE_TOKEN_RE.match(token)
        if m:
            try:
                date = datetime(
                    int(m.group(1)), int(m.group(2)),
                    int(m.group(3))).date()
            except ValueError:
                pass  # 形は合うが実在しない日付は無視する
            continue
        m = _LOG_RANGE_TOKEN_RE.match(token)
        if m:
            h1, m1, h2, m2 = (int(g) for g in m.groups())
            if h1 <= 23 and h2 <= 23 and m1 <= 59 and m2 <= 59:
                start_min = h1 * 60 + m1
                end_min = h2 * 60 + m2
            continue
        if _LOG_COUNT_TOKEN_RE.match(token):
            count = int(token)
            continue
        # 認識できない指定は無視する。
    count = max(1, min(LOG_REQUEST_MAX_COUNT, count))
    range_label = None
    if start_min is not None:
        range_label = (
            f"{start_min // 60:02d}:{start_min % 60:02d}〜"
            f"{end_min // 60:02d}:{end_min % 60:02d}")
    if date is not None and range_label is not None:
        label = f"{date.isoformat()} {range_label}"
    elif date is not None:
        label = date.isoformat()
    elif range_label is not None:
        label = range_label
    else:
        label = f"直近{count}件"
    return {"date": date, "start_min": start_min,
            "end_min": end_min, "count": count, "label": label}


def select_log_events(events, spec, now=None) -> list:
    """動作ログから、指定に合うものを選ぶ (1.96)。

    日付があればその日の分だけ、時刻範囲があれば (日付または
    今日の) その範囲内 (分単位・両端を含む) だけに絞り、
    最後の count 件を返す (並びは古い順のまま)。"""
    now = now if now is not None else datetime.now()
    spec = spec if isinstance(spec, dict) else {}
    date = spec.get("date")
    start_min = spec.get("start_min")
    end_min = spec.get("end_min")
    count = spec.get("count") or LOG_REQUEST_DEFAULT_COUNT
    range_date = date if date is not None else now.date()
    picked = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        at = e.get("at")
        if not isinstance(at, (int, float)):
            continue
        moment = datetime.fromtimestamp(float(at))
        if date is not None and moment.date() != date:
            continue
        if start_min is not None:
            if moment.date() != range_date:
                continue
            minute = moment.hour * 60 + moment.minute
            if minute < start_min or minute > end_min:
                continue
        picked.append(e)
    return picked[-count:] if count > 0 else picked


def build_log_reply(events, spec) -> str:
    """ログリクエストへの返信を組み立てる (1.96)。

    見出しは「[PHAL:動作ログ] {label}・{n}件」で、label が既に
    件数で終わっている場合 (「直近50件」) は件数を重ねない。
    中身はビューアと同じ日付つきの行。記録が無ければ
    「(記録はありません)」を添える。"""
    events = [e for e in (events or []) if isinstance(e, dict)]
    spec = spec if isinstance(spec, dict) else {}
    label = str(spec.get("label") or "")
    if label.endswith("件"):
        head = f"[PHAL:動作ログ] {label}"
    else:
        head = f"[PHAL:動作ログ] {label}・{len(events)}件"
    if not events:
        return head + "\n（記録はありません）"
    return "\n".join(
        [head] + format_log_lines(events, with_date=True))


def find_full_output(command_id, outputs_dir=None):
    """全文リクエストの対象ファイルを探す (1.91)。

    要約モードで保存された全文 (phal-outputs の
    {command_id}_{日時}_{digest}.txt、save_full_output の命名)
    のうち、更新時刻が最新のものを返す。無ければ None。
    command_id が空・パス区切りや「..」を含む不正な形なら
    None (置き場の外は探さない)。"""
    cid = str(command_id or "").strip()
    if not cid or "/" in cid or "\\" in cid or ".." in cid:
        return None
    folder = Path(outputs_dir) if outputs_dir is not None \
        else data_dir() / "phal-outputs"
    try:
        entries = [entry for entry in folder.iterdir()
                   if entry.is_file()
                   and entry.suffix == ".txt"
                   and entry.name.startswith(cid + "_")]
    except OSError:
        return None
    if not entries:
        return None

    def _mtime(entry):
        try:
            return entry.stat().st_mtime
        except OSError:
            return 0.0

    return max(entries, key=_mtime)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == resolved_root or \
        resolved.is_relative_to(resolved_root)


def resolve_file_request(request, watch_folder, work_folder):
    """ファイルリクエストのパスを、許可されたフォルダの配下の
    実ファイルへ解決する (1.88、Nagato の線引き)。

    許可するのはダウンロードフォルダ (監視フォルダ) と作業
    フォルダの配下だけ。相対パスは両方のフォルダから探し、
    絶対パスは配下にあるときだけ認める。戻り値は
    (Path, "") か (None, 理由) で、理由は "not_found" (配下に
    無い) か "outside" (絶対パスが配下の外) か "empty"。
    解決後の実パスで判定するので、「..」やリンクでの抜け出し
    は not_found / outside になる。"""
    req = str(request or "").strip()
    if not req:
        return None, "empty"
    roots = [Path(root) for root in (watch_folder, work_folder)
             if root]
    candidate = Path(req)
    if candidate.is_absolute():
        for root in roots:
            if _is_inside(candidate, root):
                resolved = candidate.resolve()
                if resolved.is_file():
                    return resolved, ""
                return None, "not_found"
        return None, "outside"
    for root in roots:
        resolved = (root / req).resolve()
        if _is_inside(resolved, root) and resolved.is_file():
            return resolved, ""
    return None, "not_found"


# ---------------------------------------------------------------
# ファイルリクエストの範囲指定 (1.99)。行の形は
# 「PHAL:ファイルリクエスト <パス> [指定...]」で、末尾の語が
# 指定の形に合う間だけ指定として切り離し、残りをパスとする
# (パスに空白が含まれていてもよいための倒し方。パス自体が
# 指定と同じ形で終わる名前のときは、指定として解釈される)。
#   lines:A-B   テキストの A行目から B行目まで (1始まり、両端を含む)
#   lines:A-    A行目から末尾まで
#   bytes:A-B   バイトの A以上 B未満 (0始まり)
#   bytes:A+N   Aから Nバイト
# bytes と lines が両方あれば bytes を優先する。
# ---------------------------------------------------------------
_FILE_LINES_SPEC_RE = re.compile(r"^lines:(\d+)(?:-(\d*))?$")
_FILE_BYTES_SPEC_RE = re.compile(r"^bytes:(\d+)(?:-(\d+)|\+(\d+))$")
FILE_SLICE_MAX_BYTES = 4_000_000  # 範囲取得で送る大きさの上限


def parse_file_request(rest):
    """ファイルリクエストの残り (パス＋指定) を分ける (1.99)。

    戻り値は (パス文字列, 指定の辞書か None)。指定の辞書は
    {"kind": "lines", "start": A, "end": B|None} (行、1始まり、
    end は含む) か {"kind": "bytes", "start": A, "end": B}
    (バイト、0始まり、end は含まない)。"""
    text = str(rest or "").strip()
    if not text:
        return "", None
    tokens = text.split()
    specs = {}
    while tokens:
        last = tokens[-1]
        match = _FILE_LINES_SPEC_RE.match(last)
        if match and "lines" not in specs:
            specs["lines"] = {
                "kind": "lines",
                "start": int(match.group(1)),
                "end": int(match.group(2)) if match.group(2)
                else None,
            }
            tokens.pop()
            continue
        match = _FILE_BYTES_SPEC_RE.match(last)
        if match and "bytes" not in specs:
            start = int(match.group(1))
            if match.group(3) is not None:
                end = start + int(match.group(3))
            else:
                end = int(match.group(2))
            specs["bytes"] = {
                "kind": "bytes", "start": start, "end": end}
            tokens.pop()
            continue
        break
    spec = specs.get("bytes") or specs.get("lines")
    return " ".join(tokens), spec


def read_byte_range(path, start, end):
    """ファイルのバイト範囲 [start, end) を読む (1.99)。

    ファイルの大きさで丸め、上限 (FILE_SLICE_MAX_BYTES) で
    切る。戻り値は (bytes, 上限で切ったか)。"""
    size = Path(path).stat().st_size
    end = min(end, size)
    start = min(start, end)
    truncated = False
    if end - start > FILE_SLICE_MAX_BYTES:
        end = start + FILE_SLICE_MAX_BYTES
        truncated = True
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(end - start), truncated


def decode_text_bytes(data: bytes):
    """バイト列をテキストとして読む (1.99)。

    BOM (UTF-8 / UTF-16) を見て、次に UTF-8、だめなら cp932。
    戻り値は (text, 文字コード名)。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace"), "cp932"


def slice_text_lines(text, spec):
    """行の範囲を取り出す (1.99)。

    戻り値は (取り出した文, 全行数)。start は1始まりで、範囲が
    全行数を超えていれば末尾までに丸める。start が全行数より
    大きければ空になる。"""
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(spec["start"]))
    end = spec["end"] if spec["end"] is not None else total
    end = min(int(end), total)
    if start > total or end < start:
        return "", total
    return "\n".join(lines[start - 1:end]), total


def looks_binary(data: bytes) -> bool:
    """先頭に NUL を含むデータはバイナリとみなす (1.99、lines
    指定の可否の判定)。"""
    return b"\x00" in data[:8192]


def slice_file_name(path, spec) -> str:
    """範囲取得の送付ファイル名 (元の名前に範囲を付ける、1.99)。"""
    target = Path(path)
    if spec["kind"] == "lines":
        end = spec["end"] if spec["end"] is not None else "end"
        suffix = f"_lines{spec['start']}-{end}"
    else:
        suffix = f"_bytes{spec['start']}-{spec['end']}"
    return target.stem + suffix + target.suffix


def build_resend_text(command_id, stored_text,
                      truncated: bool = False) -> str:
    """前回の結果の再送文を組み立てる (1.75)。

    控え (last_results) に残っているのは結果文の本文 (見出し行
    を除いた形) なので、再送と分かる見出しを付け直し、通常の
    結果と同じく末尾へ AI への指示文を付ける。"""
    head = f"[PHAL:結果再送] {command_id} （前回の結果を再送します）"
    if truncated:
        head += " ※控えは2万字で打ち切られています"
    return with_result_ai_prompt(head + "\n" + (stored_text or ""))


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


def build_status_qr_text(version, state_label, stage_label, step,
                         item_count, ran_count, error_count,
                         last_failure, automation_flags,
                         active_name, emergency, at=None,
                         link_label="") -> str:
    """状態QR の中身を組み立てる (2.09)。

    外から見えない場所 (職場の環境3など) で、PHAL の機械として
    の状態だけをスマホへ運ぶための短い文。版・状態・段階・
    手数・件数・エラーの種別・設定の入り切りだけを入れ、仕事の
    中身 (ファイル名・スクリプトの内容・ログ本文) は入れない。
    automation_flags は窓の _automation_flags と同じ辞書。
    link_label は見張る窓の表示 (2.14 の連携先の行を 2.23 で
    この行へ一本化、☑/☐ つきのプロファイル並び)。空なら
    active_name だけを出す (従来どおり)。"""
    at = at if at is not None else datetime.now()
    auto_text = " ".join(
        f"{label}={'入' if automation_flags.get(key) else '切'}"
        for key, label in _SETTINGS_AUTO_LABELS)
    lines = [
        f"[PHAL:状態] Ver{version}",
        "時刻 " + at.strftime("%Y-%m-%d %H:%M:%S"),
        f"見張る窓 {link_label or active_name or '(未設定)'}",
        f"状態 {state_label}（段階 {stage_label}）",
        f"手数 {step}",
        f"一覧 {item_count}件（実行済み {ran_count}件・"
        f"チェックエラー {error_count}件）",
        f"直近の実行失敗 {last_failure or 'なし'}",
        f"自動化 {auto_text}",
        f"非常停止 {'入' if emergency else '切'}",
    ]
    return "\n".join(lines)


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


def format_elapsed(seconds) -> str:
    """経過・所要時間の表示 (窓の上の帯用、1.48)。

    秒を「分:秒」(1時間以上は「時:分:秒」)にする。まだ一度も
    実行していないときは None を渡し、「--:--」になる。小数
    は切り捨て (帯は1秒ごとに付け直すので、表示が1秒ずつ
    進む)。"""
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------
# 段階表示 (1.63、案D)。窓の上の帯に DL → 実行 → 回答 → 送信
# の段階を並べ、今の段階を光らせる。今の段階が長引くにつれて
# 背景色が緑 → 黄 → 赤へ徐々に変わる。色の計算はここ (純粋
# 関数) に寄せ、窓は段階と経過秒を渡して色を受け取るだけに
# する。待機中は段階が無い (None) ので色は進まない。
# 物差しは段階ごと: 実行は正規に長くかかるので一番長く
# (2分あたりから黄ばみ始め、10分で赤)、他は短く (30秒あたり
# から黄ばみ始め、3分で赤)。実行の経過は「滞在時間」では
# なく「最後の出力からの時間 (無音の長さ)」を窓が渡す。
# ---------------------------------------------------------------
STAGE_ORDER = ("dl", "run", "answer", "send")
STAGE_LABELS = {"dl": "DL", "run": "実行", "answer": "回答",
                "send": "送信"}
# 段階ごとの物差し (黄ばみ始める秒, 赤になる秒)。
STAGE_RULERS = {
    "dl": (30.0, 180.0),
    "run": (120.0, 600.0),
    "answer": (30.0, 180.0),
    "send": (30.0, 180.0),
}
# 色の両端と中間 (RGB)。緑 → 黄 → 赤。
_STAGE_GREEN = (40, 167, 69)
_STAGE_YELLOW = (255, 193, 7)
_STAGE_RED = (220, 53, 69)


def stage_color_ratio(stage, elapsed) -> float:
    """段階の「長引き具合」を 0.0〜1.0 で返す。

    黄ばみ始める秒までは 0.0、赤になる秒で 1.0、その間は線形。
    知らない段階・負の経過は 0.0 (色を進めない側に倒す)。"""
    ruler = STAGE_RULERS.get(stage)
    if ruler is None:
        return 0.0
    yellow_at, red_at = ruler
    seconds = max(0.0, float(elapsed or 0.0))
    if seconds <= yellow_at:
        return 0.0
    if seconds >= red_at:
        return 1.0
    return (seconds - yellow_at) / (red_at - yellow_at)


def _stage_mix(a, b, ratio):
    return tuple(
        round(a[i] + (b[i] - a[i]) * ratio) for i in range(3))


def stage_color(stage, elapsed):
    """段階と経過秒から、帯のチップの背景色 (R, G, B) を返す。

    比率 0.0〜0.5 は緑 → 黄、0.5〜1.0 は黄 → 赤の補間。"""
    ratio = stage_color_ratio(stage, elapsed)
    if ratio <= 0.5:
        return _stage_mix(_STAGE_GREEN, _STAGE_YELLOW, ratio * 2.0)
    return _stage_mix(_STAGE_YELLOW, _STAGE_RED,
                      (ratio - 0.5) * 2.0)


# ---------------------------------------------------------------
# 進捗申告の行 (1.63)。スクリプトが phal_lib.progress(done,
# total) を呼ぶと、標準出力へ「進捗：done/total」の行が出る。
# PHAL の逐次取り込みがこの形の行を拾って帯に進捗として出し、
# 通常の出力行数には数えない (結果文にはそのまま残る: 実行の
# 記録として落とさない)。
# ---------------------------------------------------------------
_PROGRESS_LINE_RE = re.compile(
    r"^進捗[:：]\s*(\d+)\s*/\s*(\d+)\s*$")


def parse_progress_line(line):
    """進捗申告の行なら (done, total) を、違えば None を返す。

    形は「進捗：3/10」(コロンは全角・半角どちらでもよい)。
    total が 0 の行は申告として扱わない。"""
    if not isinstance(line, str):
        return None
    match = _PROGRESS_LINE_RE.match(line.strip())
    if match is None:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    if total <= 0:
        return None
    return (done, total)


# ---------------------------------------------------------------
# 導入プロンプト (1.47)。「ループを始める」でクリップボードへ
# 入れる文と、その PC の環境情報。環境は押した時点で集める
# (ベストエフォートで、取れない項目は「不明」にする)。
# ---------------------------------------------------------------
INTRO_UNKNOWN = "不明"
INTRO_MODULE_LIMIT = 12
INTRO_COMMAND_TIMEOUT_SECONDS = 10


def _run_intro_command(cmd, timeout=INTRO_COMMAND_TIMEOUT_SECONDS) -> str:
    """環境を聞くための短いコマンドを1本走らせ、標準出力を返す。

    失敗・時間切れ・0以外での終了は "" (呼ぶ側が「不明」にする)。
    PowerShell が無い環境 (VM など) では FileNotFoundError が
    出るが、ここで握りつぶす。"""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _summarize_execution_policy(raw: str) -> str:
    """Get-ExecutionPolicy -List の出力を、要点だけの1行にする。

    「Scope=Policy」の行から LocalMachine と CurrentUser を拾う。
    どちらも無ければ先頭の2行まで。空なら "" (呼ぶ側が「不明」
    にする)。"""
    pairs = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        scope, _, policy = line.partition("=")
        scope = scope.strip()
        policy = policy.strip()
        if scope and policy:
            pairs.append((scope, policy))
    if not pairs:
        return ""
    by_scope = dict(pairs)
    picked = [(scope, by_scope[scope])
              for scope in ("LocalMachine", "CurrentUser")
              if scope in by_scope]
    if not picked:
        picked = pairs[:2]
    return ", ".join(f"{scope}={policy}" for scope, policy in picked)


def _intro_modules():
    """入っているモジュール (名前と版) を集める。

    戻り値は (先頭 INTRO_MODULE_LIMIT 件の ["名前 版", ...],
    残りの件数)。標準ライブラリは配布物 (distribution) として
    現れないので、ここに出るのは入れた物だけになる。読めなければ
    (None, 0) (呼ぶ側が「不明」にする)。"""
    try:
        from importlib import metadata as _metadata

        dists = sorted(
            _metadata.distributions(),
            key=lambda d: (d.metadata["Name"] or "").lower())
    except Exception:  # noqa: BLE001  読めなければ「不明」
        return None, 0
    names = []
    for dist in dists:
        try:
            name = (dist.metadata["Name"] or "").strip()
            version = (dist.version or "").strip()
        except Exception:  # noqa: BLE001  1件読めなくても続ける
            continue
        if not name:
            continue
        names.append(f"{name} {version}" if version else name)
    return (names[:INTRO_MODULE_LIMIT],
            max(0, len(names) - INTRO_MODULE_LIMIT))


def collect_intro_environment(runner=None, watch_folder=None,
                              python_exe=None) -> dict:
    """導入プロンプトに入れる環境情報を集める。

    runner は cmd (list[str]) と timeout を受けて標準出力を返す
    呼び出し (既定は _run_intro_command)。差し替えられるのは
    テストのためで、窓側は既定のまま使う。取れない項目は
    INTRO_UNKNOWN (「不明」) にし、例外はここで止める (ボタンが
    止まらない)。"""
    run = runner if callable(runner) else _run_intro_command

    def _call(cmd):
        try:
            return (run(cmd, timeout=INTRO_COMMAND_TIMEOUT_SECONDS)
                    or "").strip()
        except Exception:  # noqa: BLE001  取れない項目は「不明」
            return ""

    exe = python_exe or sys.executable
    raw_python = _call([exe, "--version"])
    python_version = raw_python.splitlines()[0].strip() \
        if raw_python else ""
    if python_version.startswith("Python "):
        python_version = python_version[len("Python "):].strip()
    raw_ps = _call(["powershell", "-NoProfile", "-Command",
                    "$PSVersionTable.PSVersion.ToString()"])
    powershell_version = raw_ps.splitlines()[0].strip() \
        if raw_ps else ""
    policy = _summarize_execution_policy(_call(
        ["powershell", "-NoProfile", "-Command",
         "Get-ExecutionPolicy -List | ForEach-Object "
         "{ $_.Scope + '=' + $_.ExecutionPolicy }"]))
    try:
        os_version = platform.platform() or ""
    except Exception:  # noqa: BLE001  取れなければ「不明」
        os_version = ""
    try:
        encoding = locale.getpreferredencoding(False) or ""
    except Exception:  # noqa: BLE001  取れなければ「不明」
        encoding = ""
    lowered = encoding.lower()
    if "utf-8" in lowered or "utf8" in lowered:
        encoding = "UTF-8"
    elif "932" in lowered:
        encoding = "cp932"
    folder = str(watch_folder or "").strip()
    modules, modules_more = _intro_modules()
    return {
        "python_version": python_version or INTRO_UNKNOWN,
        "powershell_version": powershell_version or INTRO_UNKNOWN,
        "os_version": os_version or INTRO_UNKNOWN,
        "execution_policy": policy or INTRO_UNKNOWN,
        "watch_folder": folder or INTRO_UNKNOWN,
        "encoding": encoding or INTRO_UNKNOWN,
        "modules": modules,
        "modules_more": modules_more,
    }


# 開始プロンプトの版 (文面を変えたら上げる。履歴は AI 向け
# マニュアルの「開始プロンプト」の節に残す)。
#   第1版: 1.47 初版 / 第2版: 1.50 phal_lib の案内を追加
#   第3版: 1.78 マニュアル添付の行と、回答は .phal 限定
#   第4版: 1.79 ZIP の行 (方向が誤っていた)
#   第5版: 1.81 ZIP の行を正しい方向に直し、プロファイルで
#          有効なときだけ出す。版番号を文面に入れる。
#   第6版: 1.82 モジュールの行を削除 (後半が省略され意味が
#          無い)。フォルダの行は「ダウンロードフォルダ
#          (監視対象)」に (作業フォルダとは別という明示)。
#   第7版: 1.83 作業フォルダの行を追加 (ボタンを押したときに
#          ダイアログで選んだフォルダが入る)。
INTRO_PROMPT_VERSION = 7


def build_intro_prompt(env: dict) -> str:
    """導入プロンプトの文を組み立てる。

    env は collect_intro_environment の戻り値 (欠けたキーは
    「不明」にする)。短い日本語文と環境情報だけ。ネットに直接
    出られないことは固定文で入れる。回答は短いスクリプトで返す
    旨も入れる。env の manual_attached が真なら、マニュアルの
    ファイルを添付している旨の1行も入れる (1.78)。"""
    env = env if isinstance(env, dict) else {}

    def item(key):
        value = env.get(key)
        return value if isinstance(value, str) and value.strip() \
            else INTRO_UNKNOWN

    modules = env.get("modules")
    if modules is None:
        modules_line = INTRO_UNKNOWN
    elif not modules:
        modules_line = "なし"
    else:
        modules_line = ", ".join(modules)
        more = env.get("modules_more") or 0
        if isinstance(more, int) and more > 0:
            modules_line += f"（他 {more} 件）"
    lines = [
        f"PHAL のループを始めます。"
        f"（開始プロンプト第{INTRO_PROMPT_VERSION}版）",
    ]
    if env.get("manual_attached"):
        lines.append(
            "このメッセージには連携マニュアル"
            "（phal-ai-manual.md）を添付しています。"
            "ループの作法はそちらにあります。最初に読んでください。")
    lines += [
        "この PC の環境は次のとおりです。",
        f"- Python: {item('python_version')}",
        f"- PowerShell: {item('powershell_version')}",
        f"- OS: {item('os_version')}",
        f"- 実行ポリシー: {item('execution_policy')}",
        f"- ダウンロードフォルダ（監視対象）: {item('watch_folder')}",
    ]
    # 作業フォルダは仕事ごとに変わるので、ボタンを押したとき
    # にダイアログで選んだものだけが入る (1.83)。
    if env.get("work_folder"):
        lines.append(f"- 作業フォルダ: {env['work_folder']}")
    lines += [
        f"- 文字コード: {item('encoding')}",
        "- ネット: 直接出られません。",
        "- 実施済み試験ログの解析には共通モジュール phal_lib が使えます。",
        "- phal_lib は `import phal_lib` で読み、文字コード判定つき読み・圧縮の透過読み・要点の部品（集計・抜粋）・回の比較があります。詳しくは AI 向けマニュアルの「解析用モジュール phal_lib」の節。",
        "回答は短いスクリプトを .phal ファイルで返してください。",
    ]
    # ZIP の受け渡し (1.81 で方向を訂正)。渡せないのは
    # ユーザー → Copilot の向きだけなので、その向きの約束を
    # 伝える。プロファイルで zip_as_txt が有効な相手にだけ出す。
    if env.get("zip_as_txt"):
        lines.append(
            "- ユーザーから ZIP ファイルを渡すときは、拡張子を "
            ".zip.txt に変えて渡します。受け取ったら .txt を"
            "外して ZIP として開いてください。あなたから"
            "ユーザーへ渡すときは .zip のままで構いません。")
    return "\n".join(lines)


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
    # 探す場所は、実行時の import 探索とそろえる (中4・1-53)。
    # 先頭はスクリプトのフォルダ (実行時の sys.path[0])、次に
    # PHAL 本体のフォルダ (実行時に PYTHONPATH へ足す分。phal_lib
    # など本体の隣にあるモジュール)、残りは sys.path。
    search_path = [p for p in [payload.get("script_dir"),
                               payload.get("app_dir")] if p]
    search_path += sys.path
    for name, line in tops:
        if name in seen:
            continue
        seen.add(name)
        # スクリプト自身が実行時に作るモジュール (本文に
        # 「<名前>.py」への書き出しがある) は、検査時点では
        # まだ存在しないため見つからなくて当然で、不足には
        # 数えない (2.27、職場の Copilot の指摘)。
        if not _module_exists(name, search_path) \
                and (name + ".py") not in source:
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
    スクリプトのフォルダと PHAL 本体のフォルダ (実行時の探索と
    そろえる、1-53) を足すことで別に確かめる。"""
    payload = json.dumps({"source": source, "danger": PYTHON_DANGER,
                          "script_dir": script_dir,
                          "app_dir": str(Path(__file__).resolve().parent)})
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
        # 逐次取り込みのコールバック (1.63、execute_script の
        # on_output と同じ形)。窓が実行の表示用に差し込む。
        # execute_script の引数が優先で、無いときはこちらの
        # 持ち手から取る (呼ぶ側の差し替え口を増やさないため)。
        self.on_output = None

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


def _read_stream_lines(stream, chunks, kind, on_output):
    """パイプを1行ずつ読み、溜めながら逐次コールバックへ渡す。

    実行の逐次取り込み (1.63) の読み役 (別スレッドで回す)。
    行の区切りは \\n で、UTF-8 でも cp932 でもマルチバイト文字
    の途中に \\n は現れないため、行単位の復号で文字は割れない。
    コールバックの例外は飲む (表示の都合で実行を止めない)。"""
    try:
        while True:
            raw = stream.readline()
            if not raw:
                break
            chunks.append(raw)
            line = _decode_output(raw).rstrip("\r\n")
            try:
                on_output(kind, line)
            except Exception:  # noqa: BLE001  表示側の都合で止めない
                pass
    except (OSError, ValueError):
        pass  # パイプが閉じたら終わり


def _collect_streaming(proc, timeout, on_output, stop_tree):
    """逐次取り込みつきで子プロセスの出力を集める (1.63)。

    戻り値は (out_raw, err_raw, timed_out)。時間切れのときは
    stop_tree (ジョブを閉じて木ごと止める呼び出し) を1回だけ
    呼び、残りの出力はパイプが閉じるまで拾う。孫がパイプを
    持ち続けても、読み役はデーモンスレッドなので本体は
    待ち続けない (途中までの出力は捨てない、中5と同じ線)。"""
    import threading

    out_chunks, err_chunks = [], []
    readers = [
        threading.Thread(
            target=_read_stream_lines,
            args=(proc.stdout, out_chunks, "stdout", on_output),
            daemon=True, name="phal-run-stdout"),
        threading.Thread(
            target=_read_stream_lines,
            args=(proc.stderr, err_chunks, "stderr", on_output),
            daemon=True, name="phal-run-stderr"),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_tree()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass  # 木ごと止める手は打った。回収は読み役に任せる
    for reader in readers:
        reader.join(timeout=2.0)
    return b"".join(out_chunks), b"".join(err_chunks), timed_out


# ---------------------------------------------------------------
# ツールの実行と結果文 (1.98)。RunResult / RunHandle の
# 定義より後ろでなければならないため、置き場はここ。
# ---------------------------------------------------------------
def execute_command(argv, cwd=None, timeout_seconds=120,
                    handle: RunHandle = None,
                    on_output=None) -> RunResult:
    """コマンドラインツールを1回実行する (1.98、ブロッキング。

    窓は別スレッドから呼ぶ)。

    スクリプト実行 (execute_script) と同じ作法 (ジョブへの
    割り当て、タイムアウトで木ごと停止、逐次取り込み) で、
    任意の argv を実行する。戻り値の RunResult の name には
    表示用のコマンドラインが入る。"""
    argv = [str(a) for a in (argv or [])]
    display = subprocess.list2cmdline(argv) if argv else ""
    result = RunResult(name=display,
                       path=argv[0] if argv else "")
    if not argv:
        result.start_error = "実行するコマンドがありません"
        result.stderr = result.start_error
        return result
    timeout = _int_or(timeout_seconds, 120, 1, 3600)
    if on_output is None and handle is not None:
        on_output = getattr(handle, "on_output", None)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    started = time.monotonic()
    job = None
    proc = None
    env = dict(os.environ)
    # ツール (.py) からも phal_lib など本体の隣にあるモジュール
    # を import できるよう、本体のフォルダを PYTHONPATH の先頭
    # へ足す (2.27、スクリプト実行の env とそろえる。職場の
    # Copilot の指摘)。
    app_dir = str(Path(__file__).resolve().parent)
    existing = [p for p in (env.get("PYTHONPATH") or "").split(
        os.pathsep) if p]
    if app_dir not in existing:
        env["PYTHONPATH"] = os.pathsep.join([app_dir] + existing)
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd) if cwd else None,
            env=env,
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
        if on_output is None:
            try:
                out_raw, err_raw = proc.communicate(
                    timeout=timeout)
            except subprocess.TimeoutExpired as first:
                result.timed_out = True
                out_raw = first.output or b""
                err_raw = first.stderr or b""
                if job is not None:
                    _job_close(job)
                    job = None
                if handle is not None:
                    handle._close_job()
                kill_tree(proc)
                try:
                    out_raw, err_raw = proc.communicate(
                        timeout=10)
                except subprocess.TimeoutExpired as second:
                    if second.output:
                        out_raw = second.output
                    if second.stderr:
                        err_raw = second.stderr
        else:
            def stop_tree():
                nonlocal job
                if job is not None:
                    _job_close(job)
                    job = None
                if handle is not None:
                    handle._close_job()
                kill_tree(proc)

            out_raw, err_raw, timed_out = _collect_streaming(
                proc, timeout, on_output, stop_tree)
            if timed_out:
                result.timed_out = True
    finally:
        if job is not None:
            _job_close(job)
        if handle is not None:
            handle._close_job()
    result.seconds = time.monotonic() - started
    result.exit_code = proc.returncode if proc is not None else None
    if handle is not None and handle.stopped:
        result.stopped = True
    result.stdout = _decode_output(out_raw or b"")
    result.stderr = _decode_output(err_raw or b"")
    return result


def build_tool_result_text(display, run: RunResult,
                           config: dict) -> str:
    """ツール実行の結果文を組み立てる (1.98)。

    スクリプトの結果文と同じ形 (見出し＋出力＋エラー出力＋
    末尾の AI への指示文) で、見出しにはコマンドラインと
    終了コード・秒数を入れる。"""
    if run.start_error:
        return with_result_ai_prompt(
            f"[PHAL:ツール実行結果] {display}  起動できませんでした\n"
            f"{run.start_error}")
    code = run.exit_code if run.exit_code is not None else "?"
    head = (f"[PHAL:ツール実行結果] {display}  終了コード {code}  "
            f"{run.seconds:.1f}秒")
    if run.stopped:
        head += "  停止しました"
    elif run.timed_out:
        head += "  （時間切れで止めました）"
    parts = [head, _section("出力", run.stdout, config),
             _section("エラー出力", run.stderr, config)]
    return with_result_ai_prompt("\n".join(parts))


def execute_script(path, config: dict, handle: RunHandle = None,
                   expected_sha256: str = None,
                   on_output=None, args=None) -> RunResult:
    """スクリプトを1回実行する(ブロッキング。窓は別スレッドから呼ぶ)。

    実行してよいかの判断(F5 かどうか、要確認かどうか)は呼ぶ側の
    仕事。expected_sha256 を渡すと、起動の直前に取り直して比べ、
    違えば実行しない(低8)。記録と結果の sha256 は常に元の
    ファイルのもの。

    on_output (1.63) は逐次取り込みのコールバック (kind, line)。
    kind は "stdout" / "stderr"、line は改行を除いた1行。渡した
    ときだけ読み方を行単位の逐次取り込みに切り替え、戻りの
    RunResult の stdout / stderr は従来どおり全文になる。渡さ
    なければ従来の communicate のまま (表示が要らない経路の
    挙動は変えない)。"""
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
        argv += [str(a) for a in (args or [])]
        env = dict(os.environ)
    else:
        python = str(config.get("python") or "python")
        argv = [python, str(path)]
        argv += [str(a) for a in (args or [])]
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # 逐次取り込み (1.63) で出力がその都度届くよう、バッファ
        # リングを切る。出力の中身は変わらない (届く早さだけ)。
        env["PYTHONUNBUFFERED"] = "1"
        # PHAL 本体の置き場 (このファイルのあるフォルダ) を、子
        # プロセスの import 探索にも入れる (1-53)。phal_lib など
        # 本体の隣にあるモジュールを、スクリプトからそのまま
        # import できるようにするため。チェック側の探す場所と
        # そろえる。既存の PYTHONPATH があれば、その前へ足す。
        app_dir = str(Path(__file__).resolve().parent)
        existing = [p for p in (env.get("PYTHONPATH") or "").split(
            os.pathsep) if p]
        if app_dir not in existing:
            env["PYTHONPATH"] = os.pathsep.join([app_dir] + existing)
    if on_output is None and handle is not None:
        # 持ち手に差し込まれた逐次取り込みを使う (窓の経路)。
        on_output = getattr(handle, "on_output", None)
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
            if on_output is None:
                try:
                    out_raw, err_raw = proc.communicate(
                        timeout=timeout)
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
                        out_raw, err_raw = proc.communicate(
                            timeout=10)
                    except subprocess.TimeoutExpired as second:
                        # 孫がパイプを持ち続けて回収できなくても、
                        # 途中までの出力は捨てない(中5)。
                        if second.output:
                            out_raw = second.output
                        if second.stderr:
                            err_raw = second.stderr
            else:
                def stop_tree():
                    nonlocal job
                    if job is not None:
                        _job_close(job)  # 孫プロセスごと終わらせる
                        job = None
                    if handle is not None:
                        handle._close_job()
                    kill_tree(proc)

                out_raw, err_raw, timed_out = _collect_streaming(
                    proc, timeout, on_output, stop_tree)
                if timed_out:
                    result.timed_out = True
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


def auto_run_in_scope(name: str, auto_downloaded: bool) -> bool:
    """自動実行の対象範囲 (案3、1.63) に入るかどうか。

    対象は次のどちらかに当たるファイルだけ: (a) この PHAL が
    自動ダウンロードしたファイル (auto_downloaded は窓側が
    到着の経緯から決めて渡す)、(b) ファイル名にコマンド ID
    (「1-123_」の形) が付いたファイル。無関係なファイルの
    誤実行を防ぐための線引きで、起動前からあった分を対象に
    しない等の従来の歯止め (窓側の auto_eligible) とは別に、
    両方を満たしたものだけが自動で走る。"""
    if auto_downloaded:
        return True
    return parse_command_id(name or "") is not None


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
        first = f"[PHAL:実行結果] {label}  時間切れで止めました({timeout}秒)"
    elif run.stopped:
        first = f"[PHAL:実行結果] {label}  停止しました"
    else:
        code = run.exit_code if run.exit_code is not None else "-"
        first = f"[PHAL:実行結果] {label}  終了コード {code}  {run.seconds:.1f}秒"
    if run.start_error:
        first += f"  起動エラー: {run.start_error}"
    if total_lines is not None:
        first += f"・全{total_lines}行"
    return first


def _record_stdout_body(out_lines: list) -> str:
    """出力の本文。25行以下なら全部、超えたら先頭15行と末尾10行だけ
    見せる (要約文の出力の段とループ記録で同じ線引き)。"""
    if not out_lines:
        return "(なし)"
    if len(out_lines) <= 25:
        return "\n".join(_collapse_repeated_lines(out_lines))
    head = _collapse_repeated_lines(out_lines[:15])
    tail = _collapse_repeated_lines(out_lines[-10:])
    omitted = len(out_lines) - 25
    return "\n".join(
        head + [f"…（中略：{omitted}行）…"] + tail)


def _record_stderr_body(err_lines: list) -> str:
    """エラー出力の本文。先頭10行だけ見せる (要約文のエラー出力の
    段とループ記録で同じ線引き)。"""
    if not err_lines:
        return "(なし)"
    shown = _collapse_repeated_lines(err_lines[:10])
    body = "\n".join(shown)
    if len(err_lines) > 10:
        body += f"\n…あと{len(err_lines) - 10}行"
    return body


def _summary_stdout_section(out_lines: list) -> str:
    """要約文の出力の段。先頭15行と末尾10行だけ見せる。

    合計25行以下なら全部見せる。見出しの行数は stdout の行数。"""
    return (f"--- 出力（要約・全{len(out_lines)}行） ---\n"
            + _record_stdout_body(out_lines))


def _summary_stderr_section(err_lines: list) -> str:
    """要約文のエラー出力の段。先頭10行だけ見せる。"""
    return (f"--- エラー出力（先頭・全{len(err_lines)}行） ---\n"
            + _record_stderr_body(err_lines))


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


def _output_is_long(run: RunResult, config: dict) -> bool:
    """要約モードで全文をファイルへ置く長さかどうか。

    build_result_text と同じ線引き (summary_lines / summary_chars)。
    出力とエラー出力の合計で見る。"""
    out_lines = _split_output_lines(run.stdout)
    err_lines = _split_output_lines(run.stderr)
    total_lines = len(out_lines) + len(err_lines)
    total_chars = len(run.stdout) + len(run.stderr)
    return (total_lines > config["summary_lines"]
            or total_chars > config["summary_chars"])


def save_full_output_if_long(run: RunResult, config: dict,
                             command_id: str = None) -> str | None:
    """要約モードで全文の保存が必要なら保存してパスを返す。

    短い出力なら何もせず None。ループ記録の控えに全文のパスを残す
    ためのもの。保存に失敗しても None (build_result_text が従来
    どおり切り詰め文へ落とす)。"""
    config = normalize_config(config)
    if not config["summary_mode"]:
        return None
    if not _output_is_long(run, config):
        return None
    return save_full_output(run, config, command_id)


def build_ack_text(command_id: str, filename: str) -> str:
    """自動実行の開始を AI へ先に知らせる受領応答 (Ack) の文 (1-51)。

    結果が出る前に「受け取って実行を始める」ことだけを伝える1行。
    command_id (ファイル名先頭のコマンドID) があれば
    「[PHAL:受領] {id} {filename} の実行を始めます」、無ければ ID の
    部分を省いて「[PHAL:受領] {filename} の実行を始めます」。結果が
    遅いときに、届いていないのか実行中なのかを送り側が区別
    できるようにするためのもの。"""
    label = f"{command_id} {filename}" if command_id else filename
    return f"[PHAL:受領] {label} の実行を始めます"


def build_blocked_text(command_id: str, filename: str, reason: str) -> str:
    """チェックで実行不可になったことを AI へ知らせる文 (1-53)。

    自動実行の対象なのにチェックで止まったファイルは、受領も
    結果も出ないため、放っておくとループが無言で止まる。その
    埋め合わせに「[PHAL:実行不可] {id} {filename} はチェックで
    実行不可（{理由}）」の1行を出す。理由はチェック結果の最初
    の指摘 (無ければ見出し) を呼ぶ側が渡す。理由が空なら括弧の
    部分を省く。"""
    label = f"{command_id} {filename}" if command_id else filename
    base = f"[PHAL:実行不可] {label} はチェックで実行不可です"
    return f"{base}（{reason}）" if reason else base


# ---------------------------------------------------------------
# 結果ファイルの添付 (1.58)
# ---------------------------------------------------------------
# スクリプトが成果ファイル (抜粋・CSV・圧縮したログ等) を AI へ
# ファイルのまま渡すための約束。スクリプトはファイルを置き場
# (outbox_dir) へ書き、標準出力に「添付：ファイル名」の行を出す。
# 窓は結果文を組み立てる前に、その行を元の標準出力から取り出し
# (extract_attach_lines)、実在するファイルだけを回答に添えて
# copilot 側へ渡す (attach_file_paths)。添付行は、送る文章にも
# 要約にも残さない。
ATTACH_LINE_RE = re.compile(r"^添付\s*[:：]\s*(.+?)\s*$")


def outbox_dir() -> Path:
    """成果ファイルの置き場 (data_dir() の下の phal-outbox)。

    全文ファイルの置き場 (phal-outputs) の隣。スクリプトが
    ここへファイルを書き、「添付：ファイル名」で知らせる。"""
    return data_dir() / "phal-outbox"


def extract_attach_lines(text: str):
    """標準出力から「添付：ファイル名」の行を取り出す。

    戻り値は (添付行を除いたテキスト, ファイル名の並び)。
    コロンは全角・半角どちらでもよい。行の前後の空白は許す。
    ファイル名は名前だけのものだけを受け付ける: フォルダ区切り
    (\\ と /) を含む名前や「.」「..」は受け付けず、名前の並び
    には入れない (置き場の外を指す指定を黙って通さないため)。
    ただし、その行自体は添付の指定の行として取り除く (指定の
    行を結果文に残さない)。同じ名前が複数あれば、先の1回だけ
    数える。改行は \\n にそろえて返す (呼び側は結果文の組み立て
    に使うので、そろっていた方が扱いが一様になる)。"""
    source = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    kept = []
    names = []
    for line in source.split("\n"):
        match = ATTACH_LINE_RE.match(line.strip())
        if match is None:
            kept.append(line)
            continue
        name = match.group(1).strip()
        if not name or name in (".", "..") \
                or "\\" in name or "/" in name:
            continue  # 受け付けない指定。行だけ取り除く
        if name not in names:
            names.append(name)
    return "\n".join(kept), names


def attach_file_paths(names):
    """添付名の並びを、outbox の実ファイルへ解決する。

    戻り値は (実在するファイルのフルパスの並び, 無かった名前の
    並び)。窓が回答の段で使う: 実在するものだけを copilot 側へ
    渡し、無いものは文章の末尾に1行足して伝える。"""
    folder = outbox_dir()
    paths = []
    missing = []
    for name in names or []:
        candidate = folder / name
        if candidate.is_file():
            paths.append(str(candidate))
        else:
            missing.append(name)
    return paths, missing


# 結果文の末尾に付ける、受け取る AI への指示文 (1.55)。実行結果を
# 受けた側が「次に何をするか」をその場で決めて動けるようにする
# ためのもの。文面はユーザーの指定 (2026-11-15) で固定。人名は
# 入れない (他のチームメンバーにも使ってもらう可能性があるため、
# 同日に「陽太さん」から「ユーザー」へ変更)。
RESULT_AI_PROMPT = (
    "AIへの指示：この結果を踏まえて、次にやる作業を明確にし、"
    "即時着手できるものは直ちに着手すること。ユーザーの判断が"
    "要るものは着手せず、報告に回すこと。")


def with_result_ai_prompt(text: str) -> str:
    """結果文の末尾に AI への指示文 (RESULT_AI_PROMPT) を付ける。

    末尾の空白・改行は落としてから、指示文を最終行にする。既に
    付いていたら二重に付けない (組み立て側と送信側の両方を
    通っても1つだけになる)。"""
    body = (text or "").rstrip()
    if not body:
        return RESULT_AI_PROMPT
    if body.endswith(RESULT_AI_PROMPT):
        return body
    return body + "\n" + RESULT_AI_PROMPT


# 再実行の結果文に付ける「前回との差分」(1.56)。同じファイルを
# もう一度実行したとき、前回の出力と今回の出力を比べて、増えた
# 行・消えた行だけを抜き出す。比べるのは標準出力＋エラー出力の
# 本文で、行の集合として比べる (並び順と、同じ行の回数の違いは
# 差分にしない)。それぞれ最大 RESULT_DIFF_LIMIT 行まで。
RESULT_DIFF_LIMIT = 10


def diff_output_lines(previous_lines, current_lines):
    """出力の行同士の差分。(増えた行, 消えた行) を返す。

    増えた行は今回 (current_lines) にだけある行、消えた行は
    前回 (previous_lines) にだけある行。どちらも出てきた順に
    並べ、同じ行の重複は畳む。並び順と、同じ行の回数の違いは
    差分にしない (集合としての比べ方)。表示の打ち切りは呼ぶ側
    (build_result_diff) が行う。"""
    previous_set = set(previous_lines)
    current_set = set(current_lines)
    added = []
    seen = set()
    for line in current_lines:
        if line not in previous_set and line not in seen:
            seen.add(line)
            added.append(line)
    removed = []
    seen = set()
    for line in previous_lines:
        if line not in current_set and line not in seen:
            seen.add(line)
            removed.append(line)
    return added, removed


def result_diff_source(text: str) -> str:
    """結果文の本文から、差分の対象になる部分を返す。

    先頭の見出し行 ([PHAL:実行結果] ...) は実行のたびに秒数が変わる
    ので除き、残りをそのまま返す。再実行の前回結果の控えに
    入れるのも、この形 (見出し行なし)。"""
    return (text or "").partition("\n")[2]


def build_result_diff(previous_text, new_text,
                      previous_truncated: bool = False) -> str:
    """前回の結果文と今回の結果文の差分 (純粋関数、1.56)。

    previous_text が None (前回の結果が無い) なら、今回の全文に
    「前回結果なし」の注記を付けて返す。前回と同じなら
    「差分なし（前回と同じ結果）」の1行だけ。違うときは見出し
    「差分：追加 N行・削除 M行（前回比）」に続けて、増えた行
    (+ 付き) と消えた行 (- 付き) だけを出す (それぞれ最大
    RESULT_DIFF_LIMIT 行まで。超えた分は「ほか N行」とだけ書く)。
    行の並びは _split_output_lines でそろえるので、末尾の改行の
    有無や CRLF の違いだけでは差分にならない。前回が2万字で
    打ち切られていたら、その旨を末尾に足す。"""
    if previous_text is None:
        body = (new_text or "").rstrip("\n")
        if not body:
            return "（前回結果なし）"
        return body + "\n（前回結果なし）"
    previous_lines = _split_output_lines(previous_text)
    new_lines = _split_output_lines(new_text or "")
    if previous_lines == new_lines:
        return "差分なし（前回と同じ結果）"
    added, removed = diff_output_lines(previous_lines, new_lines)
    lines = [f"差分：追加 {len(added)}行・削除 {len(removed)}行"
             f"（前回比）"]
    for line in added[:RESULT_DIFF_LIMIT]:
        lines.append(f"+ {line}")
    if len(added) > RESULT_DIFF_LIMIT:
        lines.append(f"（ほか {len(added) - RESULT_DIFF_LIMIT}行）")
    for line in removed[:RESULT_DIFF_LIMIT]:
        lines.append(f"- {line}")
    if len(removed) > RESULT_DIFF_LIMIT:
        lines.append(f"（ほか {len(removed) - RESULT_DIFF_LIMIT}行）")
    if previous_truncated:
        lines.append("（前回の結果は2万字で打ち切られています）")
    return "\n".join(lines)


def build_result_body(name: str, run: RunResult, config: dict,
                      command_id: str = None,
                      full_output_path: str = None) -> str:
    """実行したときにクリップボードへ入れる文の本体 (AI への指示文
    を除く)。build_result_text と build_rerun_result_text の
    共通部分。

    command_id(ファイル名先頭のコマンドID)があるときは、1行目を
    「[PHAL:実行結果] 12-3 名前 …」にして、貼り返した側が対応付けられる
    ようにする。無いときの文は今までと完全に同じ。

    要約モード(summary_mode が真)で、出力とエラー出力の合計が
    summary_lines 行または summary_chars 文字を超えたら、全文は
    ファイルへ置いて要約だけを返す。短い出力は今までと完全に
    同じ文になる。全文の保存に失敗したら、従来の切り詰め文に
    落とす(要約を諦めても実行結果は必ず返す)。full_output_path を
    渡すと、長い出力のときにそのパスを使い、保存を二重にしない
    (ループ記録の控え用。渡さなければ従来どおり保存する)。"""
    config = normalize_config(config)
    label = f"{command_id} {name}" if command_id else name
    out_lines = _split_output_lines(run.stdout)
    err_lines = _split_output_lines(run.stderr)
    total_lines = len(out_lines) + len(err_lines)
    total_chars = len(run.stdout) + len(run.stderr)
    if config["summary_mode"] and (
            total_lines > config["summary_lines"]
            or total_chars > config["summary_chars"]):
        full_path = (full_output_path
                     if full_output_path is not None
                     else save_full_output(run, config, command_id))
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


def zip_as_txt_paths(paths):
    """ZIP の添付を .zip.txt の写しに差し替える (1.81)。

    ユーザー → Copilot の向きは ZIP をそのまま渡せないため、
    プロファイルで有効なとき、回答の添付のうち .zip のものを
    同じ置き場に「元の名前 + .txt」の写しを作って差し替える。
    戻り値は (差し替え後のパスの並び, [(元の名前, 新しい名前)])
    。ZIP でないものと、写しを作れなかったものは元のまま。"""
    import shutil
    result, renamed = [], []
    for path in paths or []:
        p = Path(path)
        if p.suffix.lower() == ".zip":
            copy = p.with_name(p.name + ".txt")
            try:
                shutil.copyfile(p, copy)
            except OSError:
                result.append(path)
                continue
            result.append(copy)
            renamed.append((p.name, copy.name))
        else:
            result.append(path)
    return result, renamed


def build_result_text(name: str, run: RunResult, config: dict,
                      command_id: str = None,
                      full_output_path: str = None) -> str:
    """実行したときにクリップボードへ入れる文 (本体＋AI への指示文)。

    末尾には AI への指示文 (RESULT_AI_PROMPT) が必ず付く (1.55)。
    自動・手動のどちらの送信経路もこの関数を通るので、ここで
    付ければ両経路に行き渡る。本体の組み立ては build_result_body。"""
    return with_result_ai_prompt(
        build_result_body(name, run, config,
                          command_id=command_id,
                          full_output_path=full_output_path))


def build_rerun_result_text(name: str, run: RunResult, config: dict,
                            command_id: str = None,
                            previous_text: str = None,
                            previous_truncated: bool = False,
                            full_output_path: str = None) -> str:
    """再実行のときにクリップボードへ入れる文 (1.56)。

    1行目の見出し ([PHAL:実行結果] ...) は通常の結果文と同じで、
    その下は全文ではなく前回との差分だけ (build_result_diff)。
    要約モードの長い出力は、従来どおり要約文が差分の対象になる
    (全文比較はしない)。末尾には AI への指示文 (RESULT_AI_PROMPT)
    が必ず付く。previous_text は前回の結果の控え (result_diff_source
    の形)。無ければ全文＋「前回結果なし」の注記になる。"""
    body = build_result_body(name, run, config,
                             command_id=command_id,
                             full_output_path=full_output_path)
    first = body.partition("\n")[0]
    diff = build_result_diff(previous_text, result_diff_source(body),
                             previous_truncated)
    return with_result_ai_prompt(first + "\n" + diff)


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
    parts = [f"[PHAL:差し戻し] {label}"]
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


# ---------------------------------------------------------------
# ループ記録の Markdown まとめ (1.51)
# ---------------------------------------------------------------
# 直近の実行を「手」ごとに並べた Markdown。窓が起動後の実行を
# 控え (LoopRecord の列) に残し、「記録をコピー」でクリップボード
# へ入れる。永続化はしない (開き直すと消える)。ねらいは表の概要
# なので、実行の記録の JSONL だけでは作れない (そちらには概要も
# エラー出力も全文のパスも無い)。長い出力の扱いは要約モードと
# 同じ考え: summary_lines / summary_chars を超えたら先頭・末尾と
# 行数だけにし、全文ファイルのパスを添える。
@dataclass
class LoopRecord:
    at: str = ""              # 実行が終わった時刻 "YYYY-MM-DD HH:MM:SS"
    command_id: str = ""      # ファイル名先頭のコマンドID。無ければ ""
    name: str = ""            # ファイル名
    summary: str = ""         # ねらい (表の概要)。無ければ ""
    run: RunResult = None     # 実行結果
    full_output_path: str = ""  # 要約モードで全文を置いたパス。無ければ ""


def _record_result_line(run: RunResult, config: dict) -> str:
    """1手の「結果」の行。_result_first_line の対応付け無しの版。

    終了コードと所要時間、時間切れ・停止・起動エラー・実行ポリシー
    の印を1行にする。"""
    if run.timed_out:
        timeout = _int_or(config.get("timeout_seconds"), 120, 1, 3600)
        line = f"時間切れで止めました（{timeout}秒）"
    elif run.stopped:
        line = "停止しました"
    else:
        code = run.exit_code if run.exit_code is not None else "-"
        line = f"終了コード {code}・{run.seconds:.1f}秒"
    if run.start_error:
        line += f"・起動エラー: {run.start_error}"
    if run.policy_stop:
        line += "・実行ポリシーで停止"
    return line


def _md_fence(text: str) -> str:
    """Markdown のコード囲み。文中にある一番長い ` の連なりより
    1つ長くする (出力に ``` が混ざっても囲みが壊れない)。"""
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def build_loop_record_markdown(records, config=None, limit=20) -> str:
    """直近の実行履歴を Markdown にする。

    records は LoopRecord の列 (古い順)。直近 limit 件だけを古い順
    に並べる。1手ごとにコマンドIDと時刻の見出し、ねらい、スクリプト、
    結果 (終了コード・所要時間・出力の要点) を出す。出力が長いときは
    要約モードと同じ考えで先頭・末尾と行数だけにし、全文ファイルの
    パスを添える。空なら "" (空の Markdown は作らない)。"""
    config = normalize_config(config or {})
    kept = [r for r in (records or [])
            if r is not None and r.run is not None]
    kept = kept[-limit:] if limit > 0 else []
    if not kept:
        return ""
    lines = [f"# PHAL ループ記録（直近{len(kept)}手）", ""]
    for record in kept:
        run = record.run
        heading = record.command_id or record.name
        lines.append(f"## {heading} {record.at}".rstrip())
        summary = (record.summary or "").strip()
        lines.append(f"- ねらい：{summary if summary else '（なし）'}")
        lines.append(f"- スクリプト：{record.name}")
        lines.append(f"- 結果：{_record_result_line(run, config)}")
        out_lines = _split_output_lines(run.stdout)
        err_lines = _split_output_lines(run.stderr)
        if out_lines:
            lines.append(f"- 出力（全{len(out_lines)}行）：")
            body = _record_stdout_body(out_lines)
            fence = _md_fence(body)
            lines.append(fence)
            lines.append(body)
            lines.append(fence)
        else:
            lines.append("- 出力：（なし）")
        if err_lines:
            lines.append(f"- エラー出力（全{len(err_lines)}行）：")
            err_body = _record_stderr_body(err_lines)
            err_fence = _md_fence(err_body)
            lines.append(err_fence)
            lines.append(err_body)
            lines.append(err_fence)
        if record.full_output_path:
            lines.append(f"- 全文：{record.full_output_path}")
        lines.append("")
    return "\n".join(lines)


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
