# tests/test_phal_lib.py
# 解析用共通部品 (phal_lib.py) のテスト。合成サンプル (cp932 / UTF-16 /
# gzip / zip / tar / 複数ファイルのフォルダ / バイナリ混じり) を
# 一時フォルダに作って当てる。実物のログには依存しない。
#
#   python tests/test_phal_lib.py
import gzip
import re
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import phal_lib as lib  # noqa: E402


def _write_bytes(tmp: Path, name: str, data: bytes) -> Path:
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------
# 文字コード判定つき読み取り
# ---------------------------------------------------------------
def test_read_text_auto_utf8():
    with tempfile.TemporaryDirectory() as td:
        path = _write_bytes(Path(td), "a.log", "試験 開始\n".encode("utf-8"))
        text, enc = lib.read_text_auto(path)
        assert enc == "utf-8", enc
        assert text == "試験 開始\n", text


def test_read_text_auto_utf8_bom():
    with tempfile.TemporaryDirectory() as td:
        path = _write_bytes(Path(td), "a.log", "結果\n".encode("utf-8-sig"))
        text, enc = lib.read_text_auto(path)
        assert enc == "utf-8-sig", enc
        assert text == "結果\n", text


def test_read_text_auto_utf16_le_bom():
    with tempfile.TemporaryDirectory() as td:
        # "utf-16" で書くと LE の BOM が付く。
        path = _write_bytes(Path(td), "a.log", "結果 OK\n".encode("utf-16"))
        text, enc = lib.read_text_auto(path)
        assert enc == "utf-16-le", enc
        assert text == "結果 OK\n", text


def test_read_text_auto_utf16_be_bom():
    with tempfile.TemporaryDirectory() as td:
        data = b"\xfe\xff" + "結果 NG\n".encode("utf-16-be")
        path = _write_bytes(Path(td), "a.log", data)
        text, enc = lib.read_text_auto(path)
        assert enc == "utf-16-be", enc
        assert text == "結果 NG\n", text


def test_read_text_auto_cp932():
    with tempfile.TemporaryDirectory() as td:
        raw = "試験ログ\r\nエラー発生\r\n".encode("cp932")
        path = _write_bytes(Path(td), "a.log", raw)
        text, enc = lib.read_text_auto(path)
        assert enc == "cp932", enc
        assert text == "試験ログ\r\nエラー発生\r\n", text


def test_read_text_auto_binary_does_not_raise():
    with tempfile.TemporaryDirectory() as td:
        # PNG のマジック + 高位バイト列。BOM には当たらない並びにする。
        raw = b"\x89PNG\r\n\x1a\n" + bytes(range(1, 128))
        path = _write_bytes(Path(td), "a.bin", raw)
        text, enc = lib.read_text_auto(path)
        assert isinstance(text, str) and enc == "cp932", enc


def test_iter_lines_auto_cp932_and_utf16():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p1 = _write_bytes(tmp, "a.log", "1行目\n2行目\n".encode("cp932"))
        assert list(lib.iter_lines_auto(p1)) == ["1行目", "2行目"]
        p2 = _write_bytes(tmp, "b.log", "結果\r\n次\r\n".encode("utf-16"))
        assert list(lib.iter_lines_auto(p2)) == ["結果", "次"]


# ---------------------------------------------------------------
# 圧縮の透過読み
# ---------------------------------------------------------------
def test_gzip_single_member():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "run.log.gz"
        with gzip.open(path, "wb") as fh:
            fh.write("圧縮の中身\n".encode("cp932"))
        assert lib.list_members(path) == ["run.log"]
        text, enc = lib.read_member_text(path, "run.log")
        assert enc == "cp932", enc
        assert text == "圧縮の中身\n", text


def test_zip_members_and_read():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "run.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("logs/", "")
            zf.writestr("logs/main.log", "開始\n終了\n".encode("utf-8"))
            zf.writestr("result.csv", "項目,値\n合否,合格\n".encode("cp932"))
        members = lib.list_members(path)
        assert members == ["logs/main.log", "result.csv"], members
        text, enc = lib.read_member_text(path, "logs/main.log")
        assert (text, enc) == ("開始\n終了\n", "utf-8"), (text, enc)
        text, enc = lib.read_member_text(path, "result.csv")
        assert enc == "cp932" and "合格" in text, (text, enc)


def test_tar_gz_members_and_read():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = _write_bytes(tmp, "part.txt", "部品\n".encode("utf-8"))
        path = tmp / "run.tar.gz"
        with tarfile.open(path, "w:gz") as tf:
            tf.add(src, arcname="part.txt")
        assert lib.list_members(path) == ["part.txt"]
        text, enc = lib.read_member_text(path, "part.txt")
        assert (text, enc) == ("部品\n", "utf-8"), (text, enc)


def test_plain_file_members_and_read():
    with tempfile.TemporaryDirectory() as td:
        path = _write_bytes(Path(td), "solo.log", "単体\n".encode("utf-8"))
        assert lib.list_members(path) == ["solo.log"]
        text, enc = lib.read_member_text(path, "solo.log")
        assert (text, enc) == ("単体\n", "utf-8"), (text, enc)


# ---------------------------------------------------------------
# 実施フォルダ / アーカイブの洗い出し
# ---------------------------------------------------------------
def test_inventory_folder_roles():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_bytes(tmp, "main.log", "x\n".encode("utf-8"))
        _write_bytes(tmp, "sub/result.csv", "a,b\n".encode("utf-8"))
        _write_bytes(tmp, "sub/meta.json", b"{}")
        _write_bytes(tmp, "pack.zip", b"PK\x03\x04")
        _write_bytes(tmp, "tool.exe", b"MZ")
        _write_bytes(tmp, "memo.xyz", b"?")
        entries = {e["name"]: e for e in lib.inventory(tmp)}
        assert entries["main.log"]["role"] == "本文", entries
        assert entries["sub/result.csv"]["role"] == "表", entries
        assert entries["sub/meta.json"]["role"] == "構造化", entries
        assert entries["pack.zip"]["role"] == "アーカイブ", entries
        assert entries["tool.exe"]["role"] == "バイナリ", entries
        assert entries["memo.xyz"]["role"] == "不明", entries
        assert entries["main.log"]["size"] == 2
        assert entries["main.log"]["ext"] == "log"
        assert isinstance(entries["main.log"]["modified"], float)


def test_inventory_zip_target():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "run.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.log", "中身\n".encode("utf-8"))
            zf.writestr("b.csv", "a,b\n".encode("utf-8"))
        entries = {e["name"]: e for e in lib.inventory(path)}
        assert entries["a.log"]["role"] == "本文", entries
        assert entries["a.log"]["size"] == len("中身\n".encode("utf-8"))
        assert entries["b.csv"]["role"] == "表", entries


def test_inventory_gz_target():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "run.log.gz"
        payload = "圧縮された本文\n".encode("utf-8")
        with gzip.open(path, "wb") as fh:
            fh.write(payload)
        entries = lib.inventory(path)
        assert len(entries) == 1, entries
        assert entries[0]["name"] == "run.log", entries
        assert entries[0]["role"] == "本文", entries
        assert entries[0]["size"] == len(payload), entries


def test_inventory_plain_file_target():
    with tempfile.TemporaryDirectory() as td:
        path = _write_bytes(Path(td), "one.tsv", "a\tb\n".encode("utf-8"))
        entries = lib.inventory(path)
        assert len(entries) == 1, entries
        assert entries[0]["name"] == "one.tsv"
        assert entries[0]["role"] == "表", entries


# ---------------------------------------------------------------
# 要点の部品
# ---------------------------------------------------------------
_LINES = [
    "開始します",
    "ERROR 接続に失敗",
    "処理を続けます",
    "ERROR 接続に失敗",
    "WARN 遅延しています",
    "ERROR 接続に失敗",
    "終了します",
]


def test_count_by_pattern_word_and_regex():
    counts = lib.count_by_pattern(_LINES, ["ERROR", "WARN", re.compile(r"接+")])
    assert counts["ERROR"] == 3, counts
    assert counts["WARN"] == 1, counts
    assert counts[r"接+"] == 3, counts


def test_top_repeated():
    top = lib.top_repeated(_LINES)
    assert top == [("ERROR 接続に失敗", 3)], top
    top1 = lib.top_repeated(_LINES + ["処理を続けます"], n=1)
    assert top1 == [("ERROR 接続に失敗", 3)], top1
    # 空行は数えず、1回だけの行は出さない。
    assert lib.top_repeated(["", "", "一度だけ"]) == []


def test_top_repeated_normalize():
    lines = [
        "2026-11-14 10:00:06 ERROR 応答タイムアウト",
        "2026-11-14 10:00:09 ERROR 応答タイムアウト",
        "2026-11-15 09:00:08 ERROR 応答タイムアウト",
        "INFO 計測値 123 を記録",
        "INFO 計測値 456 を記録",
        "一度だけの行",
    ]
    # 既定 (normalize=False) では、時刻・数値だけが違う行は別々に数える。
    assert lib.top_repeated(lines) == []
    # normalize=True では数字を伏せた形でまとまり、戻る行も伏せた形。
    top = lib.top_repeated(lines, normalize=True)
    assert top == [
        ("#-#-# #:#:# ERROR 応答タイムアウト", 3),
        ("INFO 計測値 # を記録", 2),
    ], top


def test_excerpt_with_context():
    hits = lib.excerpt(_LINES, "WARN", context=1)
    assert len(hits) == 1, hits
    assert hits[0]["line_no"] == 5, hits
    assert hits[0]["lines"] == [
        "ERROR 接続に失敗", "WARN 遅延しています", "ERROR 接続に失敗"], hits
    # max_hits で打ち切る。
    hits = lib.excerpt(_LINES, "ERROR", context=0, max_hits=2)
    assert [h["line_no"] for h in hits] == [2, 4], hits


def test_head_and_tail_lines():
    assert lib.head_lines(_LINES, 2) == _LINES[:2]
    assert lib.tail_lines(_LINES, 2) == _LINES[-2:]
    assert lib.head_lines(iter(_LINES), 3) == _LINES[:3]
    assert lib.tail_lines(_LINES, 99) == _LINES


# ---------------------------------------------------------------
# 回の比較
# ---------------------------------------------------------------
def test_diff_lines():
    only_a, only_b = lib.diff_lines(
        "共通\nAだけ\n重複\n重複\n", "共通\nBだけ\n重複\n")
    assert only_a == {"Aだけ"}, only_a
    assert only_b == {"Bだけ"}, only_b
    same_a, same_b = lib.diff_lines("同じ\n", "同じ\n")
    assert same_a == set() and same_b == set()


if __name__ == "__main__":
    tests = [
        test_read_text_auto_utf8,
        test_read_text_auto_utf8_bom,
        test_read_text_auto_utf16_le_bom,
        test_read_text_auto_utf16_be_bom,
        test_read_text_auto_cp932,
        test_read_text_auto_binary_does_not_raise,
        test_iter_lines_auto_cp932_and_utf16,
        test_gzip_single_member,
        test_zip_members_and_read,
        test_tar_gz_members_and_read,
        test_plain_file_members_and_read,
        test_inventory_folder_roles,
        test_inventory_zip_target,
        test_inventory_gz_target,
        test_inventory_plain_file_target,
        test_count_by_pattern_word_and_regex,
        test_top_repeated,
        test_top_repeated_normalize,
        test_excerpt_with_context,
        test_head_and_tail_lines,
        test_diff_lines,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
