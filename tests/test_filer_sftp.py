# test_filer_sftp.py
# filer_sftp(SFTP 接続先の整形と WinSCP の起動コマンド)のテスト。Qt 不要。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filer_sftp as sftp


def test_normalize_config():
    config = sftp.normalize_sftp_config({
        "winscp_path": r"C:\tools\WinSCP.exe",
        "sites": [
            {"name": "本番", "target": "sftp://user@example.jp/"},
            {"name": "保存済み", "target": "my-session"},
            {"name": "", "target": "sftp://x/"},      # 名前なしは捨てる
            {"name": "欠け", "target": ""},           # 接続先なしは捨てる
            "not-a-dict",
        ],
    })
    assert config["winscp_path"] == r"C:\tools\WinSCP.exe"
    assert [s["name"] for s in config["sites"]] == ["本番", "保存済み"]
    # 欠けた設定は既定に落ちる
    empty = sftp.normalize_sftp_config(None)
    assert empty == {"winscp_path": "WinSCP.exe", "sites": []}
    assert sftp.normalize_sftp_config({})["winscp_path"] == "WinSCP.exe"


def test_build_command():
    assert sftp.build_command("WinSCP.exe", "sftp://user@example.jp/") == \
        ["WinSCP.exe", "sftp://user@example.jp/"]
    assert sftp.build_command("", "session") == ["WinSCP.exe", "session"]
    assert sftp.winscp_available(os.path.abspath(__file__))
    assert not sftp.winscp_available(r"C:\no\such\WinSCP.exe")


ALL_TESTS = [test_normalize_config, test_build_command]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
