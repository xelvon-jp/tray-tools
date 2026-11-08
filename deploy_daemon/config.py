"""配備係の設定。常時承認の範囲はここに固定される（仕様書 第8節）。

読み込み順： 環境変数 DEPLOY_DAEMON_BASE_DIR（無ければ既定の場所）で
基底フォルダを決め、そこの config.json を読み、環境変数で上書きする。
鍵そのものは config.json に書かず、環境変数 DEPLOY_DAEMON_INTAKE_KEY
（または config.json の intake_key_env で指す変数名）から読む。
"""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REMOTES = {"tray-tools": "https://github.com/xelvon-jp/tray-tools.git"}


def default_base_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "traytools" / "deploy-daemon"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "traytools" / "deploy-daemon"
    return Path.home() / ".local" / "share" / "traytools" / "deploy-daemon"


@dataclass
class DaemonConfig:
    # 基底フォルダと、そこから決まるパス類
    base_dir: Path = field(default_factory=default_base_dir)
    checkout_dir: Path | None = None  # 実チェックアウト（必須。未設定なら起動しない）

    # 常時承認の範囲（仕様書 第8節）
    allowed_repos: tuple = ("tray-tools",)
    allowed_branches: tuple = ("main", "muse/*")
    allowed_path_prefixes: tuple = ()  # 空 = リポジトリ内すべて可
    remotes: dict = field(default_factory=lambda: dict(DEFAULT_REMOTES))

    # 反映モード。第1段の既定は "approval"：テスト合格後にステージで止まり、
    # 承認シグナル（approvals/<届け出ID>.approve ファイル）が来て初めて反映する。
    # "auto" にすると承認なしで全面自動になる（仕様書 第8節・第9節）。
    apply_mode: str = "approval"

    # テスト（検証用コピーで実行するコマンド列。未設定なら安全側に倒れて失敗扱い）
    test_commands: tuple = ()  # tuple[tuple[str, ...], ...]
    test_timeout_sec: float = 1800.0

    # 見回りと待ちの間隔
    poll_interval_sec: float = 60.0
    blocker_wait_sec: float = 30.0  # 再起動ブロッカーがある間の待ち
    blocker_retries: int = 10  # これを超えたら保留（HELD）にして人間待ち
    liveness_retries: int = 6
    liveness_interval_sec: float = 5.0
    ref_wait_retries: int = 30  # ref が見えない届け出を何回まで次回に回すか

    # 受け取り口（Worker）
    intake_url: str = ""  # 例: https://<worker>/intake
    intake_key: str = ""  # 環境変数からだけ読む。ファイルには書かない

    python_executable: str = "python"  # traytools_send.py を呼ぶ実行系

    # ---- 派生パス ----
    @property
    def staging_root(self) -> Path:
        return self.base_dir / "staging"

    def staging_dir_for(self, repo_name: str) -> Path:
        return self.staging_root / repo_name

    @property
    def state_path(self) -> Path:
        return self.base_dir / "state.json"

    @property
    def log_path(self) -> Path:
        return self.base_dir / "operations.jsonl"

    @property
    def seen_path(self) -> Path:
        return self.base_dir / "seen.json"

    @property
    def approval_dir(self) -> Path:
        """承認シグナルの置き場。<届け出ID>.approve という空ファイルが承認。"""
        return self.base_dir / "approvals"

    @property
    def stop_file(self) -> Path:
        """停止合図ファイル。これがある間は新規受け取りをしない。"""
        return self.base_dir / "stop"

    @property
    def send_script(self) -> Path | None:
        if self.checkout_dir is None:
            return None
        return self.checkout_dir / "traytools_send.py"

    def remote_for(self, repo_name: str) -> str | None:
        return self.remotes.get(repo_name)


def _as_tuple(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _commands_from(value) -> tuple:
    """test_commands の表記ゆれを吸収する。

    JSON では [["python","check.py"], ...] でも ["python check.py", ...] でもよい。
    """
    commands = []
    for item in value or ():
        if isinstance(item, str):
            commands.append(tuple(shlex.split(item)))
        else:
            commands.append(tuple(str(part) for part in item))
    return tuple(commands)


def load_config(base_dir: Path | None = None, environ: dict | None = None) -> DaemonConfig:
    """config.json と環境変数から設定を組み立てる。"""
    env = os.environ if environ is None else environ
    if base_dir is None:
        env_base = env.get("DEPLOY_DAEMON_BASE_DIR")
        base_dir = Path(env_base) if env_base else default_base_dir()
    cfg = DaemonConfig(base_dir=Path(base_dir))

    file_data = {}
    config_file = cfg.base_dir / "config.json"
    if config_file.is_file():
        file_data = json.loads(config_file.read_text(encoding="utf-8"))

    if file_data.get("checkout_dir"):
        cfg.checkout_dir = Path(file_data["checkout_dir"])
    if file_data.get("allowed_repos") is not None:
        cfg.allowed_repos = _as_tuple(file_data["allowed_repos"])
    if file_data.get("allowed_branches") is not None:
        cfg.allowed_branches = _as_tuple(file_data["allowed_branches"])
    if file_data.get("allowed_path_prefixes") is not None:
        cfg.allowed_path_prefixes = _as_tuple(file_data["allowed_path_prefixes"])
    if file_data.get("remotes"):
        cfg.remotes = dict(file_data["remotes"])
    if file_data.get("test_commands") is not None:
        cfg.test_commands = _commands_from(file_data["test_commands"])
    for key in (
        "poll_interval_sec",
        "blocker_wait_sec",
        "blocker_retries",
        "liveness_retries",
        "liveness_interval_sec",
        "ref_wait_retries",
        "test_timeout_sec",
    ):
        if file_data.get(key) is not None:
            setattr(cfg, key, type(getattr(cfg, key))(file_data[key]))
    if file_data.get("apply_mode") in ("approval", "auto"):
        cfg.apply_mode = str(file_data["apply_mode"])
    if file_data.get("intake_url"):
        cfg.intake_url = str(file_data["intake_url"])
    if file_data.get("python_executable"):
        cfg.python_executable = str(file_data["python_executable"])

    # 環境変数はファイルより優先する
    if env.get("DEPLOY_DAEMON_CHECKOUT_DIR"):
        cfg.checkout_dir = Path(env["DEPLOY_DAEMON_CHECKOUT_DIR"])
    if env.get("DEPLOY_DAEMON_INTAKE_URL"):
        cfg.intake_url = env["DEPLOY_DAEMON_INTAKE_URL"]
    if env.get("DEPLOY_DAEMON_TEST_COMMAND"):
        cfg.test_commands = (tuple(shlex.split(env["DEPLOY_DAEMON_TEST_COMMAND"])),)
    if env.get("DEPLOY_DAEMON_POLL_INTERVAL"):
        cfg.poll_interval_sec = float(env["DEPLOY_DAEMON_POLL_INTERVAL"])
    if env.get("DEPLOY_DAEMON_APPLY_MODE") in ("approval", "auto"):
        cfg.apply_mode = env["DEPLOY_DAEMON_APPLY_MODE"]
    key_env_name = str(file_data.get("intake_key_env") or "DEPLOY_DAEMON_INTAKE_KEY")
    cfg.intake_key = env.get(key_env_name, "")
    return cfg
