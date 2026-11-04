# filer_zipupdate.py
# ZIP 展開で入れた PC のための、GitHub の ZIP からの更新(Qt に依存しない部分)。
#
# 流れ: GitHub から main の ZIP をダウンロード(だめなら手元の ZIP ファイルでも可)
#   → 中身を検証 → 今のフォルダとの差分(追加・上書き・削除)を計画
#   → 控えを取ってから適用 → 依存(requirements.txt)が変わっていれば pip install
#   → 入れたファイルの一覧(manifest)を覚える。
#
# 削除は manifest にあるものだけ(前回の ZIP 更新で入れたファイルの一覧)。
# 初回は manifest が無いので削除はしない。settings.json や *.lnk は ZIP にも
# manifest にも含まれないので、決して触らない。
# 適用は「隣の一時ファイルへ書いてから置き換える」ので、書きかけのファイルを
# 残さない。失敗したときは、どこまで進んだかと控えの場所を例外に載せる。
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

ARCHIVE_URL = "https://github.com/xelvon-jp/tray-tools/archive/refs/heads/main.zip"
MARKERS = ("main.py", "filer.py", "requirements.txt")
MANIFEST_NAME = "zip_install.json"
BACKUP_DIR_NAME = "update_backup"
DOWNLOAD_TIMEOUT = 30
PIP_TIMEOUT = 600
MAX_TOTAL_SIZE = 512 * 1024 * 1024     # 展開後の合計の上限(異常な ZIP を弾く)
CREATE_NO_WINDOW = 0x08000000


class ZipUpdateError(Exception):
    pass


# ---------------------------------------------------------------
# ダウンロード
# ---------------------------------------------------------------
def _fetch_bytes(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "tray-tools-affx"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_archive(url=ARCHIVE_URL, fetch=None, timeout=DOWNLOAD_TIMEOUT) -> Path:
    """GitHub の ZIP を一時ファイルへ落とす。失敗は ZipUpdateError(理由つき)。"""
    fetch = fetch or _fetch_bytes
    try:
        data = fetch(url, timeout)
    except urllib.error.HTTPError as e:
        raise ZipUpdateError(f"ダウンロードに失敗しました（HTTP {e.code}）") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise ZipUpdateError(f"ダウンロードに失敗しました（{reason}）。"
                             "ネットワークを確かめてください") from e
    if not data:
        raise ZipUpdateError("ダウンロードした ZIP が空でした")
    handle, name = tempfile.mkstemp(prefix="affx_update_", suffix=".zip")
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(data)
    except OSError as e:
        raise ZipUpdateError(f"ダウンロードした ZIP を保存できません: {e}") from e
    return Path(name)


# ---------------------------------------------------------------
# 検証
# ---------------------------------------------------------------
def inspect_archive(zip_path) -> dict:
    """GitHub の ZIP かを検証し、{top, label, entries} を返す。

    entries は {相対パス(区切りは /): {"size", "crc", "date_time"}}。
    最上位フォルダが1つだけ・目印のファイルがある・外へ出るエントリが無い、
    のどれかが崩れていたら ZipUpdateError。"""
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise ZipUpdateError(f"ZIP を開けません: {e}") from e
    with archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        if not files:
            raise ZipUpdateError("ZIP にファイルがありません")
        tops = set()
        entries = {}
        total = 0
        newest_py = None
        for info in files:
            name = info.filename.replace("\\", "/")
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if len(parts) < 2 or any(part == ".." for part in parts):
                raise ZipUpdateError(f"想定外のエントリがあります: {info.filename}")
            tops.add(parts[0])
            rel = "/".join(parts[1:])
            total += info.file_size
            if total > MAX_TOTAL_SIZE:
                raise ZipUpdateError("ZIP の中身が大きすぎます")
            entries[rel] = {"size": info.file_size, "crc": info.CRC,
                            "date_time": tuple(info.date_time)}
            if rel.endswith(".py"):
                stamp = tuple(info.date_time)
                if newest_py is None or stamp > newest_py:
                    newest_py = stamp
        if len(tops) != 1:
            raise ZipUpdateError("GitHub の ZIP の形ではありません"
                                 "（最上位フォルダが1つではありません）")
        missing = [name for name in MARKERS if name not in entries]
        if missing:
            raise ZipUpdateError("tray-tools の ZIP ではありません"
                                 f"（{'・'.join(missing)} がありません）")
        label = ""
        if newest_py:
            label = f"ZIP {datetime(*newest_py):%Y%m%d}"
        return {"top": next(iter(tops)), "label": label, "entries": entries}


# ---------------------------------------------------------------
# manifest(前回の ZIP 更新で入れたファイルの一覧)
# ---------------------------------------------------------------
def _data_dir(store_dir=None) -> Path:
    if store_dir is not None:
        return Path(store_dir)
    import filer_core

    return Path(filer_core.data_dir())


def load_manifest(store_dir=None) -> dict:
    """{\"files\": [相対パス], \"label\": str}。無い・壊れていれば空。"""
    try:
        data = json.loads((_data_dir(store_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": [], "label": ""}
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return {"files": [], "label": ""}
    return {"files": [str(name) for name in data["files"]],
            "label": str(data.get("label") or "")}


def save_manifest(files, label, store_dir=None) -> None:
    folder = _data_dir(store_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / MANIFEST_NAME
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps({"files": sorted(files), "label": label},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, target)


# ---------------------------------------------------------------
# 差分の計画
# ---------------------------------------------------------------
def _file_crc(path: Path) -> int:
    crc = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 256)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def _protected(rel: str) -> bool:
    """決して触らないもの(保険。ZIP にも manifest にも本来含まれない)。"""
    base = rel.rsplit("/", 1)[-1]
    return base == "settings.json" or base.startswith("settings.json.") \
        or base.lower().endswith(".lnk")


def plan_update(info: dict, repo_dir, manifest_files) -> dict:
    """{add, overwrite, delete, unchanged(件数), requirements_changed}。

    追加・上書きは ZIP の中身とディスクをサイズ→CRC の順で比べて決める。
    削除は manifest にあって新しい ZIP に無く、ディスクに残っているものだけ。"""
    repo = Path(repo_dir)
    entries = info["entries"]
    add, overwrite = [], []
    unchanged = 0
    for rel, meta in entries.items():
        target = repo / rel
        if not target.is_file():
            add.append(rel)
            continue
        try:
            if target.stat().st_size == meta["size"] and _file_crc(target) == meta["crc"]:
                unchanged += 1
                continue
        except OSError:
            pass
        overwrite.append(rel)
    delete = []
    for rel in manifest_files or []:
        rel = str(rel).replace("\\", "/")
        if rel in entries or _protected(rel):
            continue
        if (repo / rel).is_file():
            delete.append(rel)
    return {"add": sorted(add), "overwrite": sorted(overwrite),
            "delete": sorted(delete), "unchanged": unchanged,
            "requirements_changed": "requirements.txt" in add
            or "requirements.txt" in overwrite}


def plan_is_empty(plan: dict) -> bool:
    return not (plan["add"] or plan["overwrite"] or plan["delete"])


# ---------------------------------------------------------------
# 適用
# ---------------------------------------------------------------
def _make_backup(plan: dict, repo: Path, backup_root: Path, now: datetime) -> str:
    """上書き・削除されるファイルの今の内容を ZIP に控える。対象が無ければ ""。"""
    targets = [rel for rel in plan["overwrite"] + plan["delete"]
               if (repo / rel).is_file()]
    if not targets:
        return ""
    backup_root.mkdir(parents=True, exist_ok=True)
    base = f"update_backup_{now:%Y%m%d_%H%M%S}"
    path = backup_root / f"{base}.zip"
    counter = 2
    while path.exists():
        path = backup_root / f"{base}-{counter}.zip"
        counter += 1
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for rel in targets:
            out.write(repo / rel, rel)
    return str(path)


def apply_files(zip_path, info: dict, plan: dict, repo_dir, backup_root=None,
                now=None) -> dict:
    """控えを取り、ZIP の内容を repo_dir へ反映する。{added, overwritten, deleted, backup}。

    途中で失敗したら ZipUpdateError(進み具合と控えの場所つき)。控えから手で戻せる。"""
    repo = Path(repo_dir)
    now = now or datetime.now()
    if backup_root is None:
        backup_root = _data_dir() / BACKUP_DIR_NAME
    backup = _make_backup(plan, repo, Path(backup_root), now)
    done = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            by_rel = {}
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                parts = [p for p in entry.filename.replace("\\", "/").split("/")
                         if p not in ("", ".")]
                by_rel["/".join(parts[1:])] = entry
            for rel in plan["add"] + plan["overwrite"]:
                target = repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + ".affx-new")
                with archive.open(by_rel[rel]) as src, open(temp, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 256)
                        if not chunk:
                            break
                        dst.write(chunk)
                stamp = datetime(*info["entries"][rel]["date_time"]).timestamp()
                os.utime(temp, (stamp, stamp))
                os.replace(temp, target)
                done.append(rel)
        for rel in plan["delete"]:
            target = repo / rel
            target.unlink()
            done.append(rel)
            parent = target.parent
            while parent != repo and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        note = f"（{len(done)}件まで反映済み"
        note += f"。控え: {backup}）" if backup else "）"
        raise ZipUpdateError(f"更新の途中で失敗しました: {e}{note}") from e
    return {"added": len(plan["add"]), "overwritten": len(plan["overwrite"]),
            "deleted": len(plan["delete"]), "backup": backup}


def pip_install_requirements(requirements_path, run=None) -> dict:
    """venv の Python で pip install -r requirements.txt。{ok, message}。"""
    if run is None:
        def run(args, timeout):
            proc = subprocess.run(
                args, capture_output=True, timeout=timeout,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
            return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)
    try:
        code, out, err = run([sys.executable, "-m", "pip", "install", "-r",
                              str(requirements_path)], PIP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "message": f"pip を実行できません: {e}"}
    if code != 0:
        tail = (err or out or "").strip().splitlines()
        return {"ok": False, "message": tail[-1] if tail else "pip install に失敗しました"}
    return {"ok": True, "message": "依存パッケージを更新しました"}


def _decode(data) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------
# 呼ぶ側(あっふ)の入口: 確認と適用
# ---------------------------------------------------------------
def check(repo_dir, zip_path=None, fetch=None, store_dir=None) -> dict:
    """ダウンロード(または手元の ZIP)を見て、計画まで作る。

    {"state": "ready"|"uptodate"|"error", "message", "can_pick_file",
     "zip_path", "archive", "plan", "label"}"""
    downloaded = zip_path is None
    try:
        if downloaded:
            zip_path = download_archive(fetch=fetch)
        info = inspect_archive(zip_path)
    except ZipUpdateError as e:
        return {"state": "error", "message": str(e), "can_pick_file": downloaded,
                "zip_path": str(zip_path) if zip_path else ""}
    manifest = load_manifest(store_dir)
    plan = plan_update(info, repo_dir, manifest["files"])
    return {"state": "uptodate" if plan_is_empty(plan) else "ready",
            "zip_path": str(zip_path), "archive": info, "plan": plan,
            "label": info["label"],
            "manifest_present": bool(manifest["files"])}


def apply(prepared: dict, repo_dir, store_dir=None, run=None) -> dict:
    """計画どおりに適用し、manifest を更新する。pip は requirements が変わったときだけ。

    {ok, message, added, overwritten, deleted, backup, pip, files}"""
    info, plan = prepared["archive"], prepared["plan"]
    try:
        result = apply_files(prepared["zip_path"], info, plan, repo_dir)
    except ZipUpdateError as e:
        return {"ok": False, "message": str(e)}
    save_manifest(list(info["entries"]), info["label"], store_dir)
    pip = None
    if plan["requirements_changed"]:
        pip = pip_install_requirements(Path(repo_dir) / "requirements.txt", run=run)
    return {"ok": True, **result, "pip": pip,
            "files": plan["add"] + plan["overwrite"] + plan["delete"]}


def discard(zip_path) -> None:
    """ダウンロードした一時 ZIP の後片付け(失敗しても何も言わない)。"""
    if not zip_path:
        return
    try:
        Path(zip_path).unlink(missing_ok=True)
    except OSError:
        pass
