# tests/test_checklist_open.py
# トレイのメニューから実機確認チェックリスト (家サーバ) を開く URL の
# 組み立て (browser_open.checklist_url) を確かめる。
#
#   python3 tests/test_checklist_open.py
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from browser_open import checklist_url  # noqa: E402


def _keys_file(td, data):
    path = os.path.join(td, "keys.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


def test_url_with_key_in_fragment():
    with tempfile.TemporaryDirectory() as td:
        path = _keys_file(td, {"checklist_key": "abc123"})
        url = checklist_url({}, keys_path=path)
        assert url == "http://127.0.0.1:8770/checklist#t=abc123", url


def test_url_without_keys_file():
    with tempfile.TemporaryDirectory() as td:
        url = checklist_url({}, keys_path=os.path.join(td, "none.json"))
        assert url == "http://127.0.0.1:8770/checklist", url


def test_url_without_checklist_key():
    with tempfile.TemporaryDirectory() as td:
        path = _keys_file(td, {"muse_token": "x"})
        url = checklist_url({}, keys_path=path)
        assert url == "http://127.0.0.1:8770/checklist", url


def test_url_base_override():
    with tempfile.TemporaryDirectory() as td:
        path = _keys_file(td, {"checklist_key": "k"})
        url = checklist_url({"checklist_url": "http://example:9/cl"},
                            keys_path=path)
        assert url == "http://example:9/cl#t=k", url


ALL_TESTS = [
    test_url_with_key_in_fragment,
    test_url_without_keys_file,
    test_url_without_checklist_key,
    test_url_base_override,
]


def main():
    failed = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"{len(ALL_TESTS) - failed}/{len(ALL_TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
