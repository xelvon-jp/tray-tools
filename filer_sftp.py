# filer_sftp.py
# SFTP 接続先の定義(settings.json の filer.sftp)と、WinSCP を起こす
# コマンドの組み立て。あっふの Alt+S が使う。
#
# あっふ自体は SFTP クライアントを持たない。ペインでリモートを直接
# 扱うと、認証・鍵・再接続まで抱えて別のアプリになってしまうため、
# 接続は実績のある WinSCP に渡す(この PC の道具の分担。WinSCP が
# 入っていない環境では起動に失敗し、その旨がログに出る)。
# target は WinSCP の保存済みセッション名か、sftp:// の URL。
import os


def normalize_sftp_config(value) -> dict:
    """settings の filer.sftp を {winscp_path, sites:[{name, target}]} に整える。

    手で編集される値なので、name / target が欠けた接続先は捨てる。"""
    config = {"winscp_path": "WinSCP.exe", "sites": []}
    if not isinstance(value, dict):
        return config
    winscp = str(value.get("winscp_path") or "").strip()
    if winscp:
        config["winscp_path"] = winscp
    sites = value.get("sites")
    if isinstance(sites, list):
        for entry in sites:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            target = str(entry.get("target") or "").strip()
            if name and target:
                config["sites"].append({"name": name, "target": target})
    return config


def build_command(winscp_path: str, target: str) -> list:
    """WinSCP の起動コマンド。[実行ファイル, 接続先] の形。"""
    return [winscp_path or "WinSCP.exe", target]


def winscp_available(winscp_path: str) -> bool:
    """WinSCP が起動できそうか。フルパスなら実在、名前だけなら PATH 上。"""
    if os.path.dirname(winscp_path):
        return os.path.isfile(winscp_path)
    import shutil
    return shutil.which(winscp_path) is not None
