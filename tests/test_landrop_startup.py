"""統合第3段: スタートアップ登録スクリプト(startup_link)。"""

import subprocess
import sys
from pathlib import Path

from landrop import startup_link as sl


def _fake_run_factory(lnk: Path):
    def fake_run(cmd, **kwargs):
        env = kwargs["env"]
        assert Path(env["LD_TARGET"]).name == "pythonw.exe"
        assert env["LD_ARGS"] == "-m landrop --tray"
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.write_bytes(b"lnk")  # PowerShell が作る .lnk の代わり
        return subprocess.CompletedProcess(cmd, 0)
    return fake_run


def test_set_startup_roundtrip(monkeypatch, tmp_path):
    lnk = tmp_path / "Startup" / "LAN Drop.lnk"
    # CREATE_NO_WINDOW は Windows 専用定数。無い環境では 0 で代用する
    monkeypatch.setattr(sl.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    # アイコン生成で作業ツリーに実ファイルを書かないよう、置き場を tmp へ
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    monkeypatch.setattr(sl, "ICON_FILE", icon)
    monkeypatch.setattr(sl.subprocess, "run", _fake_run_factory(lnk))
    assert not sl.startup_enabled(lnk)
    sl.set_startup(True, lnk)
    assert sl.startup_enabled(lnk)
    sl.set_startup(False, lnk)
    assert not sl.startup_enabled(lnk)


def test_ensure_config_copies_from_legacy(monkeypatch, tmp_path):
    new = tmp_path / "appdata" / "config.toml"
    old = tmp_path / "old" / "config.toml"
    old.parent.mkdir(parents=True)
    old.write_text('token = "abc"\n', encoding="utf-8")
    monkeypatch.setattr(sl, "config_path", lambda: new)
    got = sl.ensure_config(old)
    assert got == new and new.read_text(encoding="utf-8") == 'token = "abc"\n'
    assert old.exists()  # 移動ではなくコピー(切り戻しのため残す)


def test_ensure_config_keeps_existing(monkeypatch, tmp_path):
    new = tmp_path / "appdata" / "config.toml"
    new.parent.mkdir(parents=True)
    new.write_text('token = "mine"\n', encoding="utf-8")
    old = tmp_path / "old.toml"
    old.write_text('token = "other"\n', encoding="utf-8")
    monkeypatch.setattr(sl, "config_path", lambda: new)
    assert sl.ensure_config(old) == new
    assert 'token = "mine"' in new.read_text(encoding="utf-8")


def test_ensure_config_missing_from_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(sl, "config_path", lambda: tmp_path / "none.toml")
    try:
        sl.ensure_config(tmp_path / "ghost.toml")
    except SystemExit:
        pass
    else:
        raise AssertionError("SystemExit expected")

