# bridge_control.py
# ブリッジ(連携ステージ)から見張りへの「お願い」を置く合図ファイル。
#
# 常駐は見張り(Claude 側の監視スクリプト)を直接止められない(常駐に UIA 等を
# 持ち込まない方針)。代わりに、ここに合図を書いておき、見張りが自分の
# ポーリングのたびに読んで従う。いまある合図は「見張りの休止」だけ:
#   %LOCALAPPDATA%\traytools\muse_bridge_pause.json に {"paused": true, ...}
# 見張り側の従い方は handoff/watcher-pause-spec.md に書いてある。
# Qt に依存しない。読みは壊れていれば「休止していない」、書きは失敗しても
# 例外を出さない(合図のせいで常駐を止めない)。
import json
import os
import time
from pathlib import Path

PAUSE_PATH = (Path(os.environ.get("LOCALAPPDATA") or ".")
              / "traytools" / "muse_bridge_pause.json")

# 窓口 (front desk) が書く合図ファイル。こちらはブリッジが読む側:
# プレイブックの実行状態が入る (書き手は front_desk.activity)。
ACTIVITY_PATH = (Path(os.environ.get("LOCALAPPDATA") or ".")
                 / "traytools" / "desk_activity.json")


def read_desk_activity(path=None):
    """窓口の合図ファイルを読む。ファイルが無い・壊れている・dict でないなら None。"""
    target = Path(path) if path is not None else ACTIVITY_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _resolve(path):
    return Path(path) if path is not None else PAUSE_PATH


def is_paused(path=None) -> bool:
    """休止の合図が出ているか。ファイルが無い・壊れている・paused が真でないなら False。"""
    try:
        data = json.loads(_resolve(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("paused") is True


def set_paused(paused, path=None) -> bool:
    """合図を書き換える。書けたら True、失敗したら False(例外は出さない)。"""
    target = _resolve(path)
    record = {"paused": bool(paused), "at": time.time(), "by": "bridge"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False
