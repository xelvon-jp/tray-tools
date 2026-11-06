# test_settings_sync.py
# 起動時の設定の自動同期(settings.sync_settings_file)と、その部品
# (fill_missing / new_launcher_items)のテスト。実ファイルは一時フォルダで扱う。
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as settings_module  # noqa: E402

DEFAULT_ITEMS = settings_module.DEFAULT_SETTINGS["taskbar_widget"]["launcher_items"]


def test_fill_missing():
    """無いキーだけ足し、既存の値とリストの中身には触らない。"""
    current = {"a": {"x": 1}, "lst": ["one"]}
    defaults = {"a": {"x": 9, "y": 2}, "lst": ["one", "two"], "b": True}
    added = settings_module.fill_missing(defaults, current)
    assert current == {"a": {"x": 1, "y": 2}, "lst": ["one"], "b": True}, current
    assert [p for p, _v in added] == ["a.y", "b"], added


def test_new_launcher_items():
    """前回そろえた既定との差分だけが対象で、自分で消した項目は復活しない。"""
    old = [name for name in DEFAULT_ITEMS if name != "calendar"]
    assert settings_module.new_launcher_items(DEFAULT_ITEMS, list(old), old) == ["calendar"]
    assert settings_module.new_launcher_items(DEFAULT_ITEMS, list(old), list(DEFAULT_ITEMS)) == []
    assert settings_module.new_launcher_items(DEFAULT_ITEMS, list(DEFAULT_ITEMS), old) == []


def test_sync_missing_and_broken_file():
    """無いファイルは作らず、壊れたファイルは触らない。どちらも例外にしない。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        missing = tmp / "settings.json"
        assert settings_module.sync_settings_file(missing) == []
        assert not missing.exists()

        broken = tmp / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        assert settings_module.sync_settings_file(broken) == []
        assert broken.read_text(encoding="utf-8") == "{ not json"


def test_sync_end_to_end():
    """最小の設定が既定まで追いつき、控えが取れ、2回目は無変更で、削除は尊重される。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = tmp / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "environment": 1,
                    "taskbar_widget": {"launcher_items": ["capture", "audio"]},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        changes = settings_module.sync_settings_file(path)
        assert "taskbar_widget.launcher_items += calendar" in changes, changes
        saved = json.loads(path.read_text(encoding="utf-8"))
        widget = saved["taskbar_widget"]
        # 新しい項目は利用者の並びの後ろへ足す(既定の順へ並べ替えはしない)。
        # 既定の先頭に palette が増えたので、capture/audio の後ろに付く形になる
        expected = ["capture", "audio"] + [
            n for n in DEFAULT_ITEMS if n not in ("capture", "audio")
        ]
        assert widget["launcher_items"] == expected, widget["launcher_items"]
        assert widget["launcher_defaults_applied"] == DEFAULT_ITEMS
        assert saved["environment"] == 1  # 既存の値はそのまま
        backups = list(tmp.glob("settings.json.bak_*"))
        assert len(backups) == 1, backups

        # 2回目: 変更なし・書き込みなし・控えも増えない
        before_bytes = path.read_bytes()
        assert settings_module.sync_settings_file(path) == []
        assert path.read_bytes() == before_bytes
        assert len(list(tmp.glob("settings.json.bak_*"))) == 1

        # 利用者が calendar を消したら、次に同期しても復活しない
        saved["taskbar_widget"]["launcher_items"] = [
            n for n in saved["taskbar_widget"]["launcher_items"] if n != "calendar"
        ]
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        assert settings_module.sync_settings_file(path) == []
        saved2 = json.loads(path.read_text(encoding="utf-8"))
        assert "calendar" not in saved2["taskbar_widget"]["launcher_items"]


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

def test_sync_unexpected_exception_swallowed():
    """処理の途中で予期しない例外が出ても、投げずに空の一覧を返す(起動を止めない)。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        path.write_text(json.dumps({"environment": 1}), encoding="utf-8")
        original = settings_module.fill_missing

        def boom(defaults, current, prefix=""):
            raise RuntimeError("boom")

        settings_module.fill_missing = boom
        try:
            assert settings_module.sync_settings_file(path) == []
        finally:
            settings_module.fill_missing = original


def test_save_settings_trailing_newline():
    """setup.py の _save_settings と同じく末尾に改行を入れる(書き合いの diff ノイズ防止)。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        settings_module.save_settings({"environment": 1}, path)
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert json.loads(text) == {"environment": 1}
