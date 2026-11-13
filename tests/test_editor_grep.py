# tests/test_editor_grep.py
# フォルダ内の横断検索(editor/grep.py の純粋部分)を確かめる。
# Qt には触らない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_grep.py
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

from editor import grep as grepmod  # noqa: E402


def _tree(tmp):
    root = Path(tmp)
    (root / "a.txt").write_text("1行目\n探す語 あり\n3行目\n",
                                encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("# 探す語\nprint('探す語')\n",
                              encoding="utf-8")
    (root / "c.dat").write_text("探す語\n", encoding="utf-8")  # 対象外の拡張子
    git = root / ".git"
    git.mkdir()
    (git / "d.txt").write_text("探す語\n", encoding="utf-8")   # 歩かないフォルダ
    (root / "e.txt").write_bytes("探す語\r\n次\n".encode("cp932"))  # SJIS+CRLF
    return root


def test_grep_finds_across_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        result = grepmod.grep_folder(str(root), "探す語")
        found = {(os.path.basename(m.path), m.line) for m in result.matches}
        assert found == {("a.txt", 2), ("b.py", 1), ("b.py", 2),
                         ("e.txt", 1)}, found
        assert result.files_scanned == 3  # a.txt, b.py, e.txt
        assert not result.truncated
        line = [m.text for m in result.matches
                if os.path.basename(m.path) == "a.txt"][0]
        assert line == "探す語 あり"


def test_grep_case_sensitive():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.txt").write_text("Foo\nfoo\n", encoding="utf-8")
        result = grepmod.grep_folder(str(root), "foo", case_sensitive=True)
        assert [m.line for m in result.matches] == [2]
        result = grepmod.grep_folder(str(root), "foo")
        assert [m.line for m in result.matches] == [1, 2]


def test_grep_size_limit_and_truncation():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "big.txt").write_text("語\n" * 100, encoding="utf-8")
        (root / "small.txt").write_text("語\n", encoding="utf-8")
        # 大きさの上限を小さくすると、大きいファイルは対象から外れる。
        result = grepmod.grep_folder(str(root), "語", max_bytes=10)
        assert [os.path.basename(m.path) for m in result.matches] == ["small.txt"]
        # 件数の上限で打ち切る。
        result = grepmod.grep_folder(str(root), "語", max_results=5)
        assert len(result.matches) == 5 and result.truncated


def test_grep_progress_and_empty():
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        calls = []
        result = grepmod.grep_folder(
            str(root), "無い語",
            progress=lambda n, path: calls.append((n, path)))
        assert result.matches == []
        assert calls  # 進捗は呼ばれる
        assert grepmod.grep_folder(str(root), "").matches == []
        assert grepmod.grep_folder("", "語").matches == []


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
