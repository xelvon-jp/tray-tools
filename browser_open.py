# browser_open.py
# HTMLファイルをブラウザで開く。既定のブラウザではなく、開くブラウザを指定できる。
#
# 既定を Edge にしているのは、同梱の presenter.html が Firefox では真っ白になるため。
# あれは about:blank へ document.write して親と同一オリジンにする作りで、Firefox の
# file:// 周りの扱い(privacy.file_unique_origin など)と噛み合わない。Chrome と Edge では
# 動くことを確認済み。os.startfile は常に既定のブラウザへ渡してしまうので、ここで
# 実行ファイルを名指しする。
#
# 見つからなければ既定のブラウザへ落とす。ブラウザを名指しできないより、
# 「開くには開く」ほうがましなため。
import os
import subprocess
import sys
import winreg
from pathlib import Path

# 設定に書ける名前と、App Paths レジストリに登録されている実行ファイル名の対応。
# App Paths を引くのは、ブラウザが PATH に無いのが普通で、インストール先も
# 32bit/64bit で変わるため(Edge は Program Files (x86) 側に入る)。
_BROWSER_KEYS = {
    "edge": "msedge.exe",
    "chrome": "chrome.exe",
    "firefox": "firefox.exe",
}

DEFAULT_BROWSER = "edge"


def _from_app_paths(exe_name: str):
    """App Paths レジストリから実行ファイルのフルパスを引く。無ければ None。"""
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s" % exe_name
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if value and os.path.exists(value):
            return value
    return None


def resolve_browser(name: str):
    """設定の名前から実行ファイルのパスを決める。決められなければ None。

    name にパスが直接書かれていればそれを使う(一覧に無いブラウザや、可搬版を
    使いたいとき用)。"""
    if not name:
        return None
    if os.path.sep in name or name.lower().endswith(".exe"):
        return name if os.path.exists(name) else None
    return _from_app_paths(_BROWSER_KEYS.get(name.lower(), name))


def open_html(path, browser: str = DEFAULT_BROWSER) -> str:
    """path をブラウザで開く。使ったブラウザの名前を返す(既定へ落ちたときは "default")。

    例外は投げない。呼ぶ側はQtのスロットで、投げ切ると常駐ごと終わるため。
    失敗したときは OSError を投げるのではなく、そのまま送出する os.startfile に任せる
    (関連付けが無い場合だけはあちらが投げるので、呼ぶ側で受けること)。"""
    exe = resolve_browser(browser)
    if exe:
        try:
            # ブラウザは起動したらすぐ返る(既に起動していれば新しいタブを開いて終わる)。
            # 親から切り離す必要は無いが、こちらが終了を待つ意味も無いので Popen で投げる。
            subprocess.Popen([exe, str(Path(path).resolve())], close_fds=True)
            return browser
        except OSError as e:
            print(f"[tray-tools] {browser} で開けませんでした: {e}", file=sys.stderr)
    os.startfile(str(path))
    return "default"
