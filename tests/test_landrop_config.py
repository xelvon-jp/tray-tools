"""統合第2段: 設定の置き場（%LOCALAPPDATA%\\traytools\\landrop と旧置き場の引き継ぎ）。"""

from pathlib import Path

import pytest

from landrop import config


@pytest.fixture
def placed(monkeypatch, tmp_path):
    """LOCALAPPDATA と旧置き場を tmp に差し替える。"""
    localapp = tmp_path / "LocalAppData"
    legacy = tmp_path / "project" / "config.toml"
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))
    monkeypatch.setattr(config, "LEGACY_CONFIG_PATH", legacy)
    monkeypatch.delenv("LANDROP_CONFIG", raising=False)
    return localapp, legacy


def test_env_var_wins(monkeypatch, tmp_path, placed):
    target = tmp_path / "elsewhere.toml"
    monkeypatch.setenv("LANDROP_CONFIG", str(target))
    assert config.config_path() == target


def test_new_default_when_nothing_exists(placed):
    localapp, _ = placed
    assert config.config_path() == localapp / "traytools" / "landrop" / "config.toml"


def test_legacy_is_inherited_when_only_it_exists(placed):
    _, legacy = placed
    legacy.parent.mkdir(parents=True)
    legacy.write_text('token = "x"\n', encoding="utf-8")
    assert config.config_path() == legacy


def test_new_default_wins_once_created(placed):
    localapp, legacy = placed
    legacy.parent.mkdir(parents=True)
    legacy.write_text('token = "x"\n', encoding="utf-8")
    new = localapp / "traytools" / "landrop" / "config.toml"
    new.parent.mkdir(parents=True)
    new.write_text('token = "y"\n', encoding="utf-8")
    assert config.config_path() == new


def test_load_settings_creates_dir_and_file(placed):
    localapp, _ = placed
    settings = config.load_settings()
    created = localapp / "traytools" / "landrop" / "config.toml"
    assert created.exists()
    assert settings.token  # 初回生成でトークンが入る
