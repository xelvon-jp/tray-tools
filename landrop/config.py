"""設定の読み込み。

config.toml が無ければ、ランダムなトークン入りで自動生成する。
config.toml はトークンを含むので git 管理しない（.gitignore 済み）。
"""

from __future__ import annotations

import os
import re
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


@dataclass
class Settings:
    token: str
    save_dir: Path
    host: str = "0.0.0.0"
    port: int = 8000
    max_file_mb: int = 1024
    # QRコード・案内に出すPCのアドレス。空なら「PC名.local」、"ip" なら現在のIP
    advertise_host: str = ""

    date_folders: bool = False       # 「保存先/2026-10-17」の日付フォルダに入れる（直近の受信も覚える）
    session_gap_sec: int = 60        # この秒数あいたら次の受信は別の回（「直近」「一つ前」の区切り）
    open_folder: bool = False        # 受信したらそのフォルダを Explorer で開く
    single_image_clipboard: bool = False  # 画像が1枚だけ届いたらクリップボードにも入れる
    single_image_open: bool = False       # 画像が1枚だけ届いたらフォルダの代わりに画像を開く
    text_popup: bool = False         # iPhone から届いたテキストを PC の小さな窓でプレビュー（コピーはボタンで）
    text_to_clipboard: bool = False  # iPhone から届いたテキストを PC のクリップボードへ自動で入れる
    share_pc_clipboard: bool = False  # iPhone から PC のクリップボードの文字を取得できるようにする
    notify: bool = False             # 受信時のWindows通知
    heic_to_jpeg: bool = False       # HEIC→JPEG変換
    heic_keep_original: bool = True  # 変換元の HEIC を保存先の _heic フォルダに残す
    jpeg_quality: int = 92

    # ---- 将来機能（未実装。設定キーだけ先に予約しておく） ----
    https: bool = False          # 自己署名証明書でHTTPS化
    cert_file: str = ""
    key_file: str = ""
    mdns: bool = False           # mDNS/zeroconf で自動発見

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


def default_save_dir() -> Path:
    return Path.home() / "Downloads" / "LanDrop"


CONFIG_TEMPLATE = """\
# LAN Drop 設定ファイル（初回起動時に自動生成）
# token は iPhone 側に渡す合言葉。漏れたら "" (空) にして再起動すると作り直される。
token = "{token}"

# 受信ファイルの保存先
save_dir = '{save_dir}'

host = "0.0.0.0"
port = 8000

# 1ファイルあたりの上限（MB）
max_file_mb = 1024

# QRコード・案内に載せるPCのアドレス。
#   ""   … PC名.local（IPが変わっても使える。既定）
#   "ip" … 現在のIPアドレス（.local で届かない環境向け）
#   それ以外 … その値をそのまま使う
advertise_host = ""

# 受信したファイルは「保存先/2026-10-17」のような日付フォルダに入れる。
# 直近2回分の受信は別に覚えていて、iPhone の「PCで表示」で「直近」「一つ前」として選べる。
# session_gap_sec 秒あいたら、次の受信は別の回になる
date_folders = true
session_gap_sec = 60

# 受信が一段落したら、そのフォルダを Explorer で開く（同じフォルダは1回だけ）
open_folder = true

# 画像が1枚だけ届いたとき（トレイメニューのチェックでも切り替えられる）
single_image_clipboard = true   # クリップボードにも入れる（そのまま Ctrl+V で貼れる）
single_image_open = false       # フォルダの代わりに画像そのものを開く

# テキスト共有
text_popup = true           # iPhone から届いたテキストを PC の画面右下の窓でプレビュー（「クリップボードにコピー」で明示的にコピー）
text_to_clipboard = false   # true にすると、届いたテキストを PC のクリップボードへ自動で入れる
share_pc_clipboard = true   # iPhone のページ・ショートカットから PC のクリップボードの文字を取れる

# 受信したらWindowsの通知を出す（クリックで受信フォルダが開く）
notify = true

# HEIC/HEIF を JPEG に変換する。元の HEIC は heic_keep_original = true なら保存先の _heic フォルダへ
heic_to_jpeg = true
heic_keep_original = true
jpeg_quality = 92

# ---- 将来機能（まだ動きません） ----
https = false
cert_file = ""
key_file = ""
mdns = false
"""


def config_path() -> Path:
    return Path(os.environ.get("LANDROP_CONFIG", DEFAULT_CONFIG_PATH))


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    if not path.exists():
        path.write_text(
            CONFIG_TEMPLATE.format(token=new_token(), save_dir=default_save_dir()),
            encoding="utf-8",
        )
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)

    token = str(data.get("token", "")).strip()
    if not token:
        # 空・未記入なら作って書き戻す（他の設定はそのまま）
        token = new_token()
        line = f'token = "{token}"'
        text, n = re.subn(r"(?m)^token\s*=.*$", line, text, count=1)
        path.write_text(text if n else f"{line}\n{text}", encoding="utf-8")

    known = Settings.__dataclass_fields__.keys()
    kwargs = {k: v for k, v in data.items() if k in known}
    kwargs["token"] = token
    kwargs["save_dir"] = Path(os.path.expandvars(str(data.get("save_dir") or default_save_dir()))).expanduser()
    return Settings(**kwargs)


def new_token() -> str:
    return secrets.token_urlsafe(16)


def save_value(key: str, value: bool | int | str, path: Path | None = None) -> None:
    """config.toml の1項目だけ書き換える（コメントや他の行はそのまま）。トレイのチェック項目用。"""
    path = path or config_path()
    literal = ("true" if value else "false") if isinstance(value, bool) else (
        str(value) if isinstance(value, int) else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    )
    text = path.read_text(encoding="utf-8")
    text, n = re.subn(rf"(?m)^({re.escape(key)}\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s#]+)", lambda m: m.group(1) + literal, text, count=1)
    if not n:
        text = text.rstrip("\n") + f"\n{key} = {literal}\n"
    path.write_text(text, encoding="utf-8")
