"""スタートアップ登録(ログイン時の自動起動)と、その設定スクリプト。

トレイ本体(tray.py)からは、アイコン生成と .lnk の付け外しだけを使う。
このモジュール単体でも実行できる:

    python -m landrop.startup_link status    # 登録と設定の現状を表示
    python -m landrop.startup_link install   # .lnk を作る(向き先は実行中の python の pythonw)
    python -m landrop.startup_link install --from-config <旧 config.toml>
    python -m landrop.startup_link remove    # .lnk を消す

tray-tools の venv の python で実行すれば、.lnk の向き先はその venv の
pythonw になる。install は既存の設定を壊さない: 解決できる設定が既に
あればそのまま、無くて --from-config があれば新しい既定の置き場へ
コピーする(移動はしないので、旧環境への切り戻しがそのまま効く)。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from .config import PROJECT_ROOT, config_path, load_settings

STARTUP_LNK = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\LAN Drop.lnk"
ICON_FILE = Path(__file__).parent / "static" / "icon.ico"


def icon_image(size: int = 64) -> Image.Image:
    """青い角丸に白い「受け取り」矢印。16px に潰れても形が分かるよう太めに描く。"""
    s = size / 64
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((2 * s, 2 * s, 62 * s, 62 * s), radius=14 * s, fill=(31, 111, 235, 255))
    white = (255, 255, 255, 255)
    d.rectangle((26 * s, 10 * s, 38 * s, 32 * s), fill=white)                       # 矢印の軸
    d.polygon([(14 * s, 28 * s), (50 * s, 28 * s), (32 * s, 46 * s)], fill=white)  # 矢じり
    d.rectangle((12 * s, 50 * s, 52 * s, 55 * s), fill=white)                       # 受け皿
    return im


def write_icon_file(path: Path = ICON_FILE) -> Path:
    icon_image(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return path


# ---- スタートアップ登録 ----

def startup_enabled(lnk: Path = STARTUP_LNK) -> bool:
    return lnk.exists()


def set_startup(enabled: bool, lnk: Path = STARTUP_LNK) -> None:
    """ログイン時に `pythonw -m landrop --tray` を起動するショートカットを置く／消す。"""
    if not enabled:
        lnk.unlink(missing_ok=True)
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not ICON_FILE.exists():
        write_icon_file()
    # 値は環境変数で渡す（パスに空白や引用符があってもコマンド文字列を組み立てずに済む）
    env = {
        **os.environ,
        "LD_LNK": str(lnk),
        "LD_TARGET": str(pythonw),
        "LD_ARGS": "-m landrop --tray",
        "LD_DIR": str(PROJECT_ROOT),
        "LD_ICON": str(ICON_FILE),
    }
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LD_LNK);"
        "$s.TargetPath=$env:LD_TARGET;$s.Arguments=$env:LD_ARGS;$s.WorkingDirectory=$env:LD_DIR;"
        "$s.IconLocation=$env:LD_ICON;$s.Description='LAN Drop 受信サーバー';$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )


# ---- 設定の引き継ぎと、スクリプトとしての動作 ----


def ensure_config(from_config: Path | None = None) -> Path:
    """引き継ぎ後の設定ファイルの場所を返す。

    解決できる設定が既にあればそれを使う。無くて from_config があれば、
    新しい既定の置き場へコピーする(元は残す)。どちらも無ければ、解決先
    (まだ存在しない既定の場所)を返し、初回起動時の自動生成に任せる。
    """
    resolved = config_path()
    if resolved.exists():
        return resolved
    if from_config is not None:
        if not from_config.exists():
            raise SystemExit(f"引き継ぎ元の設定が見つかりません: {from_config}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(from_config, resolved)
    return resolved


def describe_config(path: Path) -> str:
    if not path.exists():
        return f"設定: {path} (まだ無い。初回起動時にトークン自動生成)"
    s = load_settings(path)
    return f"設定: {path} (トークンあり、保存先 {s.save_dir}、ポート {s.port})"


def cmd_status() -> int:
    print(f"スタートアップ登録: {'あり' if startup_enabled() else 'なし'} ({STARTUP_LNK})")
    print(describe_config(config_path()))
    return 0


def cmd_install(from_config: Path | None) -> int:
    cfg = ensure_config(from_config)
    set_startup(True)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    print(f"登録しました: {STARTUP_LNK}")
    print(f"向き先: {pythonw} -m landrop --tray (作業フォルダ {PROJECT_ROOT})")
    print(describe_config(cfg))
    return 0


def cmd_remove() -> int:
    set_startup(False)
    print(f"登録を消しました: {STARTUP_LNK}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LAN Drop のスタートアップ登録")
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install", help=".lnk を作る")
    p_install.add_argument("--from-config", type=Path, default=None,
                           help="引き継ぐ旧設定ファイル(config.toml)の場所")
    sub.add_parser("remove", help=".lnk を消す")
    sub.add_parser("status", help="登録と設定の現状を表示")
    args = parser.parse_args(argv)
    if args.command == "install":
        return cmd_install(args.from_config)
    if args.command == "remove":
        return cmd_remove()
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
