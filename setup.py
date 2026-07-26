# setup.py
# 別のPCへ tray-tools を展開するためのセットアップ。何度実行しても壊れない(冪等)。
#
# PowerShell スクリプト(.ps1)ではなく Python で書いてあるのは、企業PCでは実行ポリシーが
# GPO で固定されていて .ps1 の実行自体が禁止されていることがあるため。ショートカット作成だけは
# WScript.Shell が要るので powershell -Command のインライン実行に頼る(実行ポリシーが規制するのは
# スクリプトファイルの実行なので、-Command なら通ることが多い)。
#
# 管理者権限が要る操作はしない。venv もスタートアップ登録もユーザープロファイル配下で完結する。
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = Path.home() / ".venvs" / "tray-tools"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
VENV_PYTHONW = VENV_DIR / "Scripts" / "pythonw.exe"
REQUIREMENTS = PROJECT_DIR / "requirements.txt"
SETTINGS_PATH = PROJECT_DIR / "settings.json"
SHORTCUT_PATH = PROJECT_DIR / "TrayTools.lnk"
ICON_PATH = PROJECT_DIR / "icons" / "rapture.ico"

MIN_PYTHON = (3, 10)

# デバイスに順番に振る色。足りなくなったら先頭へ戻る。
_ICON_COLORS = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#0891b2"]
_HEADPHONE_HINTS = ["ヘッドホン", "ヘッドセット", "イヤホン", "headphone", "headset", "earphone"]

# venv 側の python で走らせて、有効な出力デバイスを JSON で返させる。
# setup.py 自体はシステムの python で動くので pycaw を import できない。
_DETECT_CODE = """
import json
from pycaw.constants import AudioDeviceState, EDataFlow
from pycaw.utils import AudioUtilities

found = []
for device in AudioUtilities.GetAllDevices():
    if device.state != AudioDeviceState.Active:
        continue
    try:
        if AudioUtilities.GetEndpointDataFlow(device.id, 1) != EDataFlow.eRender.value:
            continue
    except Exception:
        continue
    found.append({"label": str(device.FriendlyName), "id": device.id})
print(json.dumps(found, ensure_ascii=False))
"""


def _step(number: int, title: str) -> None:
    print()
    print(f"[{number}/6] {title}")


def _ask_yes_no(question: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix}: ").strip().lower()
    except EOFError:
        # 対話できない環境(パイプ経由など)では既定に倒す
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


# ---------------------------------------------------------------
# 1. Python のバージョン
# ---------------------------------------------------------------
def check_python() -> None:
    _step(1, "Python のバージョンを確認します")
    current = sys.version_info
    print(f"  実行中: Python {current.major}.{current.minor}.{current.micro} ({sys.executable})")
    if current < MIN_PYTHON:
        print()
        print(f"  中止: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です。")
        print("  このアプリは PySide6 を使っており、古い Python では対応する版が入りません。")
        print("  https://www.python.org/downloads/windows/ からユーザー領域へインストールし直してください")
        print("  (インストーラの 'Install for all users' は管理者権限が要るので外してください)。")
        sys.exit(1)
    print("  OK")


# ---------------------------------------------------------------
# 2. venv
# ---------------------------------------------------------------
def ensure_venv() -> None:
    _step(2, "仮想環境(venv)を用意します")
    print(f"  場所: {VENV_DIR}")
    if VENV_PYTHON.exists():
        print("  既にあるのでそのまま使います")
        return
    # プロジェクトフォルダの外に作る。OneDrive 等に同期されると、再生成できるだけの
    # 5000ファイル以上が丸ごとクラウドへ上がってしまうため。
    print("  作成中... (数十秒かかります)")
    result = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)])
    if result.returncode != 0 or not VENV_PYTHON.exists():
        print()
        print("  中止: venv の作成に失敗しました。")
        print(f"  次を手で実行して、エラー内容を確認してください: {sys.executable} -m venv {VENV_DIR}")
        sys.exit(1)
    print("  作成しました")


# ---------------------------------------------------------------
# 3. 依存パッケージ
# ---------------------------------------------------------------
def install_requirements() -> None:
    _step(3, "依存パッケージをインストールします")
    if not REQUIREMENTS.exists():
        print(f"  中止: {REQUIREMENTS} が見つかりません")
        sys.exit(1)
    print("  pip install -r requirements.txt を実行します (数分かかります)")
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    )
    if result.returncode != 0:
        print()
        print("  中止: pip でのインストールに失敗しました。")
        print("  社内ネットワークではプロキシで外部への接続が弾かれることがあります。")
        print("  プロキシがある環境では、次のように --proxy を付けて実行してください:")
        print()
        print(f'    "{VENV_PYTHON}" -m pip install --proxy http://ユーザー名:パスワード@プロキシ:ポート -r requirements.txt')
        print()
        print("  プロキシのアドレスとポートは、ブラウザの設定か情報システム部門に確認してください。")
        print("  SSL 証明書のエラーが出る場合は、社内の証明書ファイルを pip に渡す必要があります:")
        print(f'    "{VENV_PYTHON}" -m pip install --cert C:\\path\\to\\corp-ca.pem -r requirements.txt')
        print()
        print("  解決したら setup.py をもう一度実行してください(途中まで済んだ分はやり直しません)。")
        sys.exit(1)
    print("  OK")


# ---------------------------------------------------------------
# 4. 音声デバイスと settings.json
# ---------------------------------------------------------------
def detect_devices() -> list:
    """有効な音声出力デバイスの [{label, id}] を返す。取得できなければ空リスト。"""
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", _DETECT_CODE],
        capture_output=True,
        # 日本語のデバイス名が化けないよう、子プロセスの出力を UTF-8 に固定する
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []


def _guess_icon(label: str) -> str:
    lowered = label.lower()
    return "headphone" if any(h in lowered for h in _HEADPHONE_HINTS) else "monitor"


def _build_devices(detected: list) -> list:
    devices = []
    for index, item in enumerate(detected):
        devices.append({
            "label": item["label"],
            "id": item["id"],
            "icon": _guess_icon(item["label"]),
            "color": _ICON_COLORS[index % len(_ICON_COLORS)],
        })
    return devices


def setup_settings() -> None:
    _step(4, "音声デバイスを検出して settings.json を用意します")
    detected = detect_devices()
    if detected:
        print("  検出した出力デバイス:")
        for index, item in enumerate(detected, start=1):
            print(f"    {index}. {item['label']}")
            print(f"       {item['id']}")
    else:
        print("  出力デバイスを検出できませんでした(後から手で設定できます)")

    if SETTINGS_PATH.exists():
        # 既存の設定は絶対に壊さない。デバイス構成を変えたい場合だけ手で編集してもらう。
        print(f"  {SETTINGS_PATH.name} が既にあるので、既存の設定をそのまま使います")
        print("  デバイスを入れ替えたい場合は、上の一覧の id を settings.json の audio.devices に書いてください")
        return

    sys.path.insert(0, str(PROJECT_DIR))
    import settings as settings_module

    new_settings = json.loads(json.dumps(settings_module.DEFAULT_SETTINGS))
    new_settings["audio"]["devices"] = _build_devices(detected)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(new_settings, f, ensure_ascii=False, indent=2)
    print(f"  {SETTINGS_PATH} を作成しました(デバイス {len(new_settings['audio']['devices'])} 件)")
    print(f"  キャプチャの保存先: {new_settings['capture']['save_folder']}")


# ---------------------------------------------------------------
# 5. ショートカット
# ---------------------------------------------------------------
def _ps_literal(text: str) -> str:
    """PowerShell の単一引用符文字列にする。中の ' は '' に重ねてエスケープする。"""
    return "'" + str(text).replace("'", "''") + "'"


def create_shortcut(lnk_path: Path) -> bool:
    target = VENV_PYTHONW if VENV_PYTHONW.exists() else VENV_PYTHON
    parts = [
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_literal(lnk_path)})",
        f"$s.TargetPath = {_ps_literal(target)}",
        # コマンド文字列に " を直接書くと powershell へ渡る途中の引数分割で壊れるため、
        # パスを囲む二重引用符は [char]34 で組み立てる(空白入りパス対策)。
        f"$s.Arguments = [char]34 + {_ps_literal(PROJECT_DIR / 'main.py')} + [char]34",
        f"$s.WorkingDirectory = {_ps_literal(PROJECT_DIR)}",
        f"$s.Description = 'tray-tools'",
    ]
    if ICON_PATH.exists():
        parts.append(f"$s.IconLocation = {_ps_literal(ICON_PATH)}")
    parts.append("$s.Save()")

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(parts)],
            capture_output=True,
        )
    except OSError as e:
        print(f"  ショートカットを作成できませんでした: {e}")
        return False
    if result.returncode != 0 or not lnk_path.exists():
        print("  ショートカットを作成できませんでした。")
        message = result.stderr.decode("utf-8", errors="replace").strip()
        if message:
            print(f"  {message.splitlines()[0]}")
        return False
    return True


def setup_shortcut() -> None:
    _step(5, "起動用ショートカットを作成します")
    if create_shortcut(SHORTCUT_PATH):
        print(f"  {SHORTCUT_PATH} を作成しました")
    else:
        print("  代わりに TrayTools-debug.bat から起動できます(コンソールが出ます)")


# ---------------------------------------------------------------
# 6. スタートアップ登録
# ---------------------------------------------------------------
def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def setup_startup(choice) -> None:
    _step(6, "サインイン時の自動起動を設定します")
    startup_lnk = startup_dir() / "TrayTools.lnk"
    already = startup_lnk.exists()
    if already:
        print("  既にスタートアップに登録されています")

    if choice is None:
        # 仕事用PCでは勝手に常駐させたくないことがあるので、既定は「登録しない」。
        choice = _ask_yes_no("  サインイン時に自動起動しますか?", default=False)

    if not choice:
        print("  自動起動は設定しません")
        if already:
            print(f"  やめる場合は次のファイルを削除してください: {startup_lnk}")
        return

    startup_lnk.parent.mkdir(parents=True, exist_ok=True)
    if create_shortcut(startup_lnk):
        print(f"  登録しました: {startup_lnk}")
    else:
        print(f"  手動で登録する場合は、TrayTools.lnk を次のフォルダへコピーしてください: {startup_lnk.parent}")


# ---------------------------------------------------------------
def print_next_steps() -> None:
    print()
    print("=" * 60)
    print("セットアップが完了しました。")
    print()
    print("起動する:")
    print(f"  {SHORTCUT_PATH}  をダブルクリック (コンソールなし)")
    print(f"  {PROJECT_DIR / 'TrayTools-debug.bat'}  (うまく動かないときの確認用)")
    print()
    print("音声デバイスを設定し直す:")
    print(f'  "{VENV_PYTHON}" list_devices.py   でIDの一覧を表示し、')
    print(f"  {SETTINGS_PATH} の audio.devices に label と id を書く")
    print("  (トレイアイコンの右クリックメニュー「設定」からも同じファイルを開けます)")
    print()
    print("ホットキーを無効にする:")
    print("  settings.json の hotkeys の値を空文字 \"\" にする")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="tray-tools のセットアップ")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--startup", dest="startup", action="store_true", help="自動起動を登録する(確認を省略)")
    group.add_argument("--no-startup", dest="startup", action="store_false", help="自動起動を登録しない(確認を省略)")
    parser.set_defaults(startup=None)
    args = parser.parse_args()

    print("tray-tools セットアップ")
    print(f"  プロジェクト: {PROJECT_DIR}")

    check_python()
    ensure_venv()
    install_requirements()
    setup_settings()
    setup_shortcut()
    setup_startup(args.startup)
    print_next_steps()


if __name__ == "__main__":
    main()
