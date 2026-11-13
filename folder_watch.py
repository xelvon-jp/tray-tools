# folder_watch.py
# 特定フォルダを定期的に見て、入ってきたファイルを別のフォルダへ移動する
# 「フォルダ監視移動」の、Qt に依存しない純ロジック側。
#
# 仕様(2026-11-05 に本人が承認した文面どおり):
#   - ルールごとに 元フォルダ / 先フォルダ / 動作間隔(秒) / 除外条件 を持つ。複数可
#   - 対象は元フォルダ直下のファイルのみ(サブフォルダとその中身は触らない)。
#     隠し・システム属性のファイルも対象外
#   - 除外条件は「除外する拡張子」と「除外する名前パターン(ワイルドカード)」。
#     生成中ファイルの拡張子(crdownload 等)は初期値として入っているだけで、
#     ハードコードではないので、設定から消せば対象に戻る
#   - 生成中判定は二重にする。拡張子に加えて「安定判定」: 初めて見た回では
#     移動せず、サイズと更新時刻を記録だけする。次のスキャンで両方が前回と
#     完全に同じだったものだけを書き込み完了とみなして移動する。
#     まだ伸びている・ロックされているファイルはその回は見送り、次回再試行する
#   - 先フォルダが無ければ作る。同名があれば上書きせず「名前 (1).拡張子」と連番を付ける
#   - 元と先が同じ、または先が元の中にあるルールは無効(移動が無限ループになるため)
#
# タイマーとメニューとダイアログは feature_screen.py / folder_watch_settings.py 側。
# ここは os.scandir と shutil.move だけで動き、テストから直接叩ける。
import fnmatch
import json
import os
import shutil
from pathlib import Path

SECTION = "folder_watch"

# 生成中ファイルの拡張子(初期値)。ブラウザごとに違う:
#   crdownload … Chrome / Edge(Chromium 系)
#   part       … Firefox
#   partial    … 一部ブラウザ・ダウンローダ
#   download   … Safari(.download は本来フォルダだが、ファイルとして現れる場合もある)
#   opdownload … Opera
#   tmp / temp … 汎用の生成中・一時ファイル
DEFAULT_EXCLUDE_EXTENSIONS = [
    "crdownload", "part", "partial", "download", "opdownload", "tmp", "temp",
]

DEFAULT_INTERVAL_SECONDS = 30
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 86400

# Windows のファイル属性。Linux では st_file_attributes が無いので 0 扱いになる。
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


def defaults() -> dict:
    """settings.py の既定値(DEFAULT_SETTINGS["folder_watch"])の写し。"""
    import settings as settings_module
    return json.loads(json.dumps(settings_module.DEFAULT_SETTINGS[SECTION]))


def folder_key(path: str) -> str:
    """同じフォルダかどうかを比べる鍵(slideshow_core.folder_key と同じ流儀)。"""
    return os.path.normcase(os.path.normpath(path))


def _clean_extensions(value) -> list:
    """拡張子の並びをそろえる。 " .CRDownload, tmp " のような書き方も受ける。"""
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str):
            continue
        ext = item.strip().lower().lstrip(".")
        if ext and ext not in result:
            result.append(ext)
    return result


def _clean_patterns(value) -> list:
    """名前パターンの並びをそろえる(空要素と重なりは落とす)。"""
    if isinstance(value, str):
        value = [part for chunk in value.splitlines() for part in chunk.split(",")]
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str):
            continue
        pattern = item.strip()
        if pattern and pattern not in result:
            result.append(pattern)
    return result


def parse_extensions_text(text: str) -> list:
    """ダイアログの入力欄(カンマ区切り)を拡張子の並びにする。"""
    return _clean_extensions(text or "")


def extensions_text(extensions) -> str:
    return ", ".join(_clean_extensions(extensions))


def parse_patterns_text(text: str) -> list:
    """ダイアログの入力欄(カンマ区切り)を名前パターンの並びにする。"""
    return _clean_patterns(text or "")


def patterns_text(patterns) -> str:
    return ", ".join(_clean_patterns(patterns))


def _interval(value) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return min(max(seconds, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS)


def normalize_rule(raw) -> dict | None:
    """ルール1件をそろえる。元・先のどちらかが空なら None(使えないルール)。"""
    if not isinstance(raw, dict):
        return None
    source = raw.get("source")
    dest = raw.get("dest")
    if not isinstance(source, str) or not source.strip():
        return None
    if not isinstance(dest, str) or not dest.strip():
        return None
    return {
        "source": source.strip(),
        "dest": dest.strip(),
        "interval_seconds": _interval(raw.get("interval_seconds")),
        # 除外拡張子がキーごと無いときだけ初期値を入れる。空リストは「除外しない」
        # という本人の指定なので、そのまま尊重する。
        "exclude_extensions": _clean_extensions(
            raw["exclude_extensions"] if "exclude_extensions" in raw
            else DEFAULT_EXCLUDE_EXTENSIONS),
        "exclude_patterns": _clean_patterns(raw.get("exclude_patterns")),
        "enabled": bool(raw.get("enabled", True)),
    }


def normalize_rules(value) -> list:
    result = []
    seen = set()
    for raw in value if isinstance(value, list) else []:
        rule = normalize_rule(raw)
        if rule is None:
            continue
        key = (folder_key(rule["source"]), folder_key(rule["dest"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(rule)
    return result


def normalize_config(section) -> dict:
    section = section if isinstance(section, dict) else {}
    return {"rules": normalize_rules(section.get("rules"))}


def read_section(settings_path) -> dict:
    """settings.json の folder_watch セクション(生のまま)。無い・壊れていれば {}。"""
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return {}
    section = stored.get(SECTION) if isinstance(stored, dict) else None
    return section if isinstance(section, dict) else {}


def load_config(settings_path) -> dict:
    return normalize_config(read_section(settings_path) if settings_path else {})


def save_config(settings_path, updates: dict, app_settings=None) -> None:
    """folder_watch の中の updates のキーだけを書く(slideshow_core.save_config と同じ流儀)。"""
    if isinstance(app_settings, dict):
        section = app_settings.get(SECTION)
        if not isinstance(section, dict):
            section = app_settings[SECTION] = {}
        section.update(json.loads(json.dumps(updates)))
    if not settings_path:
        return
    import settings as settings_module

    settings_module.update_settings_section(SECTION, updates, settings_path)


def validate_rule(rule: dict) -> str | None:
    """ルールが安全に動けるか。動けない理由(日本語1行)か None を返す。

    元と先が同じ・先が元の中、の2つは移動したファイルがまた対象になって
    無限ループになるので止める。それ以外(元の不在など)は scan 側で都度返す。"""
    source_key = folder_key(rule["source"])
    dest_key = folder_key(rule["dest"])
    if source_key == dest_key:
        return "元フォルダと先フォルダが同じです"
    try:
        common = os.path.commonpath([source_key, dest_key])
    except ValueError:
        common = None  # 別ドライブ同士など。内包関係にはならない
    if common == source_key:
        return "先フォルダが元フォルダの中にあります"
    return None


def is_excluded(name: str, rule: dict) -> bool:
    """ファイル名が除外条件に当たるか。拡張子と名前パターン、大文字小文字は区別しない。"""
    lowered = name.lower()
    if "." in name and not name.endswith("."):
        ext = name.rsplit(".", 1)[1].lower()
        if ext in rule.get("exclude_extensions", []):
            return True
    for pattern in rule.get("exclude_patterns", []):
        if fnmatch.fnmatchcase(lowered, pattern.lower()):
            return True
    return False


def unique_dest_path(dest_dir: Path, name: str) -> Path:
    """先フォルダでの置き先。同名があれば「名前 (1).拡張子」の形で連番を付ける。"""
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    if "." in name[1:]:  # 先頭のドット(隠し名)だけの名前は拡張子として扱わない
        stem, _dot, ext = name.rpartition(".")
        suffix = f".{ext}" if ext else ""
    else:
        stem, suffix = name, ""
    number = 1
    while True:
        candidate = dest_dir / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
        number += 1


class FolderWatcher:
    """ルール1件ぶんの監視状態(前回スキャンの署名)を持ち、scan_once で1回分動く。

    署名は {パスの鍵: (サイズ, 更新時刻ns)}。初めて見たファイルは署名を記録する
    だけで移動しない。次のスキャンで署名が一致したものだけが移動対象になる。"""

    def __init__(self, rule: dict):
        self.rule = rule
        self._seen: dict[str, tuple[int, int]] = {}

    def watcher_key(self) -> str:
        return folder_key(self.rule["source"]) + "->" + folder_key(self.rule["dest"])

    def scan_once(self) -> dict:
        """1回スキャンして、安定したファイルを移動する。

        戻り値: {"moved": [(元パス, 先パス), ...], "error": 理由 or None}
        落ちないこと。常駐のタイマーから呼ばれるので、例外はここで受けて
        error に載せる(呼ぶ側が通知・記録の出し分けをする)。"""
        result = {"moved": [], "error": None}
        problem = validate_rule(self.rule)
        if problem:
            result["error"] = problem
            return result
        source = Path(self.rule["source"])
        if not source.is_dir():
            result["error"] = f"元フォルダが見つかりません: {self.rule['source']}"
            return result
        try:
            entries = sorted(source.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            result["error"] = f"元フォルダを読めません: {e}"
            return result

        dest_dir = Path(self.rule["dest"])
        present = set()
        for path in entries:
            try:
                if path.is_dir():
                    continue  # フォルダは対象外(中身にも入らない)
                stat = path.stat()
            except OSError:
                continue  # スキャンの途中で消えた等。その回は見送る
            attributes = getattr(stat, "st_file_attributes", 0)
            if attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                continue
            if is_excluded(path.name, self.rule):
                continue
            key = folder_key(str(path))
            present.add(key)
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = self._seen.get(key)
            self._seen[key] = signature
            if previous != signature:
                continue  # 初見、またはまだ伸びている。次回に判定する
            # 安定したので移動する
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = unique_dest_path(dest_dir, path.name)
                shutil.move(str(path), str(target))
            except OSError:
                # ロック中など。その回は見送る。署名は残してあるので、
                # 次のスキャンで(まだ同じなら)再試行される
                continue
            self._seen.pop(key, None)
            present.discard(key)
            result["moved"].append((str(path), str(target)))
        # 消えたファイルの署名は捨てる(残すと辞書が増え続ける)
        for key in list(self._seen):
            if key not in present:
                del self._seen[key]
        return result
