# test_setup.py
# setup.py の設定更新のうち、縦ランチャの新項目を足す部分のテスト。
# 実ファイルへの読み書きは一時フォルダの settings.json で行う。
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup as setup_module  # noqa: E402
import settings as settings_module  # noqa: E402

DEFAULT_ITEMS = settings_module.DEFAULT_SETTINGS["taskbar_widget"]["launcher_items"]


def test_new_launcher_items_first_run():
    """記録が無い初回は、既定にあって今のリストに無いものが全部、既定の順で対象。"""
    current = ["capture", "audio"]
    added = setup_module._new_launcher_items(DEFAULT_ITEMS, current, [])
    assert added == [name for name in DEFAULT_ITEMS if name not in current], added


def test_new_launcher_items_respects_removals():
    """前回の既定と今の既定が同じなら、自分で消した項目は復活しない。"""
    current = [name for name in DEFAULT_ITEMS if name != "presenter"]
    assert setup_module._new_launcher_items(DEFAULT_ITEMS, current, list(DEFAULT_ITEMS)) == []


def test_new_launcher_items_only_new_defaults():
    """前回の実行のあとで既定に増えた分だけが対象になる。"""
    old_defaults = [name for name in DEFAULT_ITEMS if name != "calendar"]
    added = setup_module._new_launcher_items(DEFAULT_ITEMS, list(old_defaults), old_defaults)
    assert added == ["calendar"], added
    # 既にリストにあるものは二重に足さない
    assert setup_module._new_launcher_items(DEFAULT_ITEMS, list(DEFAULT_ITEMS), old_defaults) == []


def _run_update(tmp: Path, payload: dict) -> dict:
    path = tmp / "settings.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    original = setup_module.SETTINGS_PATH
    setup_module.SETTINGS_PATH = path
    try:
        setup_module._update_existing_settings([], False, None)
    finally:
        setup_module.SETTINGS_PATH = original
    return json.loads(path.read_text(encoding="utf-8"))


def test_update_settings_end_to_end():
    """実ファイルで: 初回は calendar まで足り、消した項目は二度と戻らない。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        payload = {
            "environment": 1,
            "taskbar_widget": {"enabled": True, "launcher_items": ["capture", "audio"]},
        }
        saved = _run_update(tmp, payload)
        widget = saved["taskbar_widget"]
        assert widget["launcher_items"] == DEFAULT_ITEMS, widget["launcher_items"]
        assert widget["launcher_defaults_applied"] == DEFAULT_ITEMS
        assert list(tmp.glob("settings.json.bak_*")), "書き換え前の控えが無い"

        # 利用者が presenter と calendar を消した、という状態で再実行しても戻らない
        widget["launcher_items"] = [
            name for name in widget["launcher_items"] if name not in ("presenter", "calendar")
        ]
        saved2 = _run_update(tmp, saved)
        items2 = saved2["taskbar_widget"]["launcher_items"]
        assert "presenter" not in items2 and "calendar" not in items2, items2

        # もう一度流しても何も変わらない(冪等)
        saved3 = _run_update(tmp, saved2)
        assert saved3["taskbar_widget"]["launcher_items"] == items2


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
