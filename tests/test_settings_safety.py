# test_settings_safety.py
# settings.json の安全な書き方のテスト: 原子的な書き込み(save_settings)、
# 壊れたファイルの退避と保存停止、キー単位の更新(update_settings)。
# 実ファイルは一時フォルダで扱う。
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as settings_module  # noqa: E402


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_save_is_atomic_and_keeps_one_bak():
    """2回目の保存で .bak に1回目の中身が残り、一時ファイルが残らない。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        settings_module.save_settings({"a": 1}, path)
        assert _read(path) == {"a": 1}
        settings_module.save_settings({"a": 2}, path)
        assert _read(path) == {"a": 2}
        bak = Path(str(path) + ".bak")
        assert _read(bak) == {"a": 1}, bak
        leftovers = [p.name for p in Path(td).iterdir() if ".tmp-" in p.name]
        assert leftovers == [], leftovers


def test_broken_file_quarantined_and_saves_stopped():
    """壊れたファイルは退避され、既定値で動き、以後そのファイルへは書かない。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        path.write_text("{ not json", encoding="utf-8")
        loaded = settings_module.load_settings(path)
        assert loaded == settings_module.DEFAULT_SETTINGS
        assert not path.exists()
        quarantined = list(Path(td).glob("settings.json.broken-*"))
        assert len(quarantined) == 1, quarantined
        assert quarantined[0].read_text(encoding="utf-8") == "{ not json"
        assert settings_module.settings_load_error(path) == quarantined[0]
        # 保存は止まっている: 既定値で上書きして設定を消さない
        settings_module.save_settings({"a": 1}, path)
        assert not path.exists()
        try:
            settings_module.update_settings({"a": 1}, path)
        except settings_module.SettingsError:
            pass
        else:
            raise AssertionError("update_settings should refuse a broken file")
        assert not path.exists()


def test_nondict_file_treated_as_broken():
    """JSON として読めても辞書でなければ壊れたものとして退避する。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        settings_module.load_settings(path)
        assert not path.exists()
        assert len(list(Path(td).glob("settings.json.broken-*"))) == 1


def test_update_settings_touches_only_given_keys():
    """生のファイルの該当キーだけ変わり、既定値しか無いキーは書き込まれない。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        path.write_text(
            json.dumps({"mine": 1, "other": {"x": 2}}), encoding="utf-8"
        )
        raw = settings_module.update_settings({"mine": 9, "new_key": [1, 2]}, path)
        assert raw == {"mine": 9, "other": {"x": 2}, "new_key": [1, 2]}, raw
        assert _read(path) == raw
        # 既定値にあるが触っていないキーはファイルに焼き込まれない
        assert "capture" not in _read(path)


def test_update_settings_creates_missing_file_with_only_given_keys():
    """ファイルが無いときは、渡したキーだけのファイルができる。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        raw = settings_module.update_settings({"a": 1}, path)
        assert raw == {"a": 1}
        assert _read(path) == {"a": 1}
        assert not Path(str(path) + ".lock").exists()


def test_update_settings_lock_contention_and_stale():
    """新しいロックがある間は SettingsError、古いロックは残骸として捨てる。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        lock = Path(str(path) + ".lock")
        lock.write_text("99999 0\n", encoding="utf-8")
        try:
            settings_module.update_settings({"a": 1}, path, lock_timeout=0.3)
        except settings_module.SettingsError:
            pass
        else:
            raise AssertionError("update_settings should fail while locked")
        # 120秒前のロックは残骸として捨てられる
        old = time.time() - 120
        os.utime(lock, (old, old))
        raw = settings_module.update_settings({"a": 1}, path, lock_timeout=2.0)
        assert raw == {"a": 1}
        assert not lock.exists()


def test_update_settings_callable_and_abort():
    """関数形: 生の辞書を書き換えられる。False を返したら書かない。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        path.write_text(json.dumps({"sec": {"a": 1}}), encoding="utf-8")

        def bump(raw):
            raw["sec"]["a"] = 2

        raw = settings_module.update_settings(bump, path)
        assert raw == {"sec": {"a": 2}}
        assert _read(path) == {"sec": {"a": 2}}

        def abort(raw):
            raw["sec"]["a"] = 99
            return False

        raw = settings_module.update_settings(abort, path)
        assert raw == {"sec": {"a": 99}}  # 戻り値は渡した辞書そのまま
        assert _read(path) == {"sec": {"a": 2}}  # ファイルは変わらない


def test_update_settings_section():
    """節の中だけ差し替わる。節が無ければ作られ、他の節・キーは残る。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        path.write_text(
            json.dumps({"sec": {"keep": 1, "chg": 1}, "top": True}),
            encoding="utf-8",
        )
        settings_module.update_settings_section("sec", {"chg": 2, "new": 3}, path)
        assert _read(path) == {"sec": {"keep": 1, "chg": 2, "new": 3}, "top": True}
        settings_module.update_settings_section("fresh", {"x": 1}, path)
        assert _read(path)["fresh"] == {"x": 1}
        # 節の値が辞書でなければ、辞書に置き換えてから差し替える
        path.write_text(json.dumps({"sec": 5}), encoding="utf-8")
        settings_module.update_settings_section("sec", {"x": 1}, path)
        assert _read(path) == {"sec": {"x": 1}}


def test_update_settings_detaches_values():
    """呼び出し側の辞書を後から変えても、書いた内容は変わらない。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        value = {"items": ["a"]}
        settings_module.update_settings({"k": value}, path)
        value["items"].append("b")
        assert _read(path) == {"k": {"items": ["a"]}}


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
