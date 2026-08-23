# settings.py
# settings.json の読み書きを行う。存在しない場合は自動生成し、壊れている場合は
# デフォルトにフォールバックする(rapture-py/settings.py の挙動を踏襲)。
import json
import time
from pathlib import Path

# 既定の保存先。ドライブ直下(C:\bak 等)は権限の無いPCでは作成に失敗しうるので、
# 必ず書けるユーザープロファイル配下にする。
DEFAULT_SAVE_FOLDER = str(Path.home() / "Pictures" / "rapture")

# 音声デバイスIDはPCごとに異なるGUIDなので、既定値としては持てない(他PCで使うと
# 存在しないIDになる)。setup.py での検出か list_devices.py の出力から設定する。
DEFAULT_SETTINGS = {
    "capture": {
        "hide_duration_ms": 2000,
        "save_folder": DEFAULT_SAVE_FOLDER,
        "save_format": "png",
        "jpeg_quality": 90,
        "history_days": 0,
        "pen_color": "#ff0000",
        "pen_width": 3,
        "highlighter_enabled": False,
    },
    "screen": {
        # スリープ抑止サブメニューに並べる時限(分)。「無期限」と「解除」は常に付く。
        "keep_awake_minutes": [30, 120],
    },
    "audio": {
        "devices": [],
    },
    # フォルダブックマーク。bookmarks は {"name": 表示名, "path": フォルダパス} の配列で、
    # アプリからの登録で増える(削除・並べ替えは settings.json を直接編集する想定)。
    # target は移動先の決め方: auto(呼んだ時の前面がエクスプローラならそこ、でなければ
    # あふｗ) / afxw(常にあふｗ) / explorer(常にエクスプローラ)。
    "launcher": {
        "target": "auto",
        "afxw_path": r"C:\soft\afxw\AFXW.EXE",
        "bookmarks": [],
    },
    # 定型文。recent は最近コピーしたテンプレート名(新しいものが先頭・上限20件)で、
    # ピッカーの並び順に使う。アプリが自分で書き足す値なので手で編集する必要はない。
    "snippets": {
        "recent": [],
    },
    # 空文字にすると、そのホットキーは登録されない(無効化できる)。
    "hotkeys": {
        "audio_toggle": "ctrl+alt+h",
        "capture_now": "ctrl+alt+r",
        "capture_sequence": "ctrl+alt+s",
        "mic_mute": "ctrl+alt+m",
        "color_picker": "ctrl+alt+c",
        "always_on_top": "ctrl+alt+t",
        "snippet_picker": "ctrl+alt+v",
        "launcher": "ctrl+alt+b",
    },
}

# settings.py 自身の置き場所を基準にする。cwd に依存させない。
SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """base(デフォルト)にoverride(読み込んだ値)を再帰的に重ねる。
    セクションごと上書きすると未指定キーが消えてしまうため、辞書同士は再帰的にマージする。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(path=None) -> dict:
    """settings.json を読み込む。存在しない/壊れている場合はデフォルトにフォールバックする。"""
    target_path = Path(path) if path else SETTINGS_PATH
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # ディープコピー

    if target_path.exists():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                merged = _deep_merge(merged, loaded)
        except (json.JSONDecodeError, OSError):
            # 壊れたJSONはそのまま使わず、デフォルト値で継続する
            pass
    else:
        save_settings(merged, target_path)

    return merged


def save_settings(settings_dict: dict, path=None) -> None:
    """settings.json へ書き込む。保存先フォルダが無ければ作成する。"""
    target_path = Path(path) if path else SETTINGS_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(settings_dict, f, ensure_ascii=False, indent=2)


def cleanup_old_captures(capture_settings: dict) -> None:
    """history_days > 0 のとき、保存フォルダ内のN日以上前のキャプチャファイルを削除する。
    デフォルト(0)では何もしない=自動削除しない。
    保存形式を変えても古いファイルが取り残されないよう rapture_*.* を対象にする。"""
    history_days = capture_settings.get("history_days", 0)
    if not history_days or history_days <= 0:
        return

    save_folder = Path(capture_settings.get("save_folder", DEFAULT_SETTINGS["capture"]["save_folder"]))
    if not save_folder.exists():
        return

    cutoff = time.time() - (history_days * 86400)

    for file_path in save_folder.glob("rapture_*.*"):
        try:
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink()
        except OSError:
            # 使用中などで削除できないファイルはスキップする
            pass
