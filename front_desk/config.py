"""front_desk.config — 窓口の設定。

秘密 (窓口鍵) は設定ファイルに書かず、環境変数 FRONT_DESK_KEY から
実行時に渡す (配備係と同じ作法)。
"""
from __future__ import annotations

import dataclasses
import json
import os
from typing import Optional

from .model import REQUEST_KINDS

STATE_FILENAME = "state.json"
LOG_FILENAME = "operations.jsonl"
PROCESSED_FILENAME = "processed.json"
STOP_FILENAME = "stop"


def default_base_dir() -> str:
    """状態ファイルの置き場。%LOCALAPPDATA%\\traytools\\front-desk。"""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "traytools", "front-desk")


@dataclasses.dataclass(frozen=True)
class DeskConfig:
    # 依頼の置き場 (Worker の基底 URL)。/requests 等はここへ足す。
    worker_base_url: str = ""
    # 見回り間隔と、実行の打ち切り。
    poll_interval_sec: int = 60
    exec_timeout_sec: int = 1800
    # 状態 (desk-state) の heartbeat: 何回の見回りごとに送るか。
    state_heartbeat_polls: int = 10
    # 実行を許可する kind。第1段は読み取り確認 (inspect) だけ。
    allowed_kinds: tuple = ("inspect",)
    # tier ごとの実行コマンド (argv の列)。本文は標準入力で渡す。
    # 例: {"haiku": ["claude", "-p", "--model", "haiku"]}
    # 未設定の tier は実行できず failed で返す。
    # 具体的な作法は PC 側での実地確認 (A7) の後に確定する。
    executor_commands: dict = dataclasses.field(default_factory=dict)
    # 実行時の作業ディレクトリ (空なら窓口のカレント)。
    workdir: str = ""
    # 状態ファイルの置き場。
    base_dir: str = dataclasses.field(default_factory=default_base_dir)
    state_filename: str = STATE_FILENAME
    log_filename: str = LOG_FILENAME
    processed_filename: str = PROCESSED_FILENAME
    stop_filename: str = STOP_FILENAME

    @property
    def state_path(self) -> str:
        return os.path.join(self.base_dir, self.state_filename)

    @property
    def log_path(self) -> str:
        return os.path.join(self.base_dir, self.log_filename)

    @property
    def processed_path(self) -> str:
        return os.path.join(self.base_dir, self.processed_filename)

    @property
    def stop_path(self) -> str:
        return os.path.join(self.base_dir, self.stop_filename)

    @classmethod
    def from_json(cls, data) -> "DeskConfig":
        """検証済みの dict から作る。知らないキーは無視する。"""
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")
        kwargs = {}
        base = data.get("worker_base_url")
        if isinstance(base, str):
            kwargs["worker_base_url"] = base.rstrip("/")
        for key in ("poll_interval_sec", "exec_timeout_sec",
                    "state_heartbeat_polls"):
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool) \
                    and value > 0:
                kwargs[key] = value
        kinds = data.get("allowed_kinds")
        if isinstance(kinds, list):
            safe = tuple(k for k in kinds
                         if isinstance(k, str) and k in REQUEST_KINDS)
            kwargs["allowed_kinds"] = safe
        commands = data.get("executor_commands")
        if isinstance(commands, dict):
            safe_cmds = {}
            for tier, argv in commands.items():
                if isinstance(argv, list) and argv and all(
                        isinstance(a, str) and a for a in argv):
                    safe_cmds[str(tier)] = list(argv)
            kwargs["executor_commands"] = safe_cmds
        for key in ("workdir", "base_dir"):
            value = data.get(key)
            if isinstance(value, str):
                kwargs[key] = value
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Optional[str]) -> "DeskConfig":
        """設定ファイル (JSON) を読む。無ければ既定値。"""
        if not path:
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))
