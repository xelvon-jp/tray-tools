# phal_lib.py
# PHAL (スクリプト実行パネル) で動かす解析スクリプト用の共通部品。
# 実施済みの試験ログは置き場・形式・文字コードがばらばらなので、
# 毎回スクリプトに書き直すと出来がばらつく所 (文字コード判定、
# 圧縮ファイルの透過読み、フォルダの洗い出し、要点の集計、回の比較)
# をここに寄せる。標準ライブラリだけに依存する。
#
# 使い方の例:
#   import phal_lib
#   text, enc = phal_lib.read_text_auto(r"D:\logs\run1.log")
#   lines = text.splitlines()
#   print(phal_lib.count_by_pattern(lines, ["ERROR", "WARN"]))
#   for name in phal_lib.list_members(r"D:\logs\run2.zip"):
#       member_text, _ = phal_lib.read_member_text(r"D:\logs\run2.zip", name)
import codecs
import gzip
import os
import re
import tarfile
import zipfile
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

__all__ = [
    "decode_bytes_auto",
    "read_text_auto",
    "iter_lines_auto",
    "list_members",
    "read_member_text",
    "inventory",
    "count_by_pattern",
    "top_repeated",
    "excerpt",
    "head_lines",
    "tail_lines",
    "diff_lines",
]


# ---------------------------------------------------------------
# 文字コード判定つき読み取り
# ---------------------------------------------------------------
def _detect_encoding(sample: bytes):
    """先頭のバイト列から (報告用の名前, 開くときに使う codec) を決める。

    BOM があればそれに従う。無ければ UTF-8 で読めるか試し、
    読めなければ cp932 とみなす。sample が文字の途中で切れている
    場合は「unexpected end of data」になるので、それも UTF-8 とする。
    """
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", "utf-8-sig"
    if sample.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", "utf-16"
    if sample.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", "utf-16"
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data":
            return "utf-8", "utf-8"
        return "cp932", "cp932"
    return "utf-8", "utf-8"


def decode_bytes_auto(data: bytes):
    """バイト列を文字コード判定つきで文字列にする。(text, encoding) を返す。

    encoding は "utf-8-sig" / "utf-16-le" / "utf-16-be" / "utf-8" /
    "cp932" のいずれか。cp932 でも読めないバイト列 (バイナリ混じり)
    は、壊れた所を置換文字にして返す (落とさないことを優先する)。
    """
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[2:].decode("utf-16-be"), "utf-16-be"
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[2:].decode("utf-16-le"), "utf-16-le"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp932"), "cp932"
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace"), "cp932"


def read_text_auto(path):
    """ファイルを文字コード判定つきで丸ごと読む。(text, encoding) を返す。"""
    return decode_bytes_auto(Path(path).read_bytes())


def iter_lines_auto(path):
    """ファイルを文字コード判定つきで開き、行を1つずつ返す (改行なし)。

    文字コードは先頭 64KB で判定する。途中で文字コードが変わる
    ファイルは想定しない。
    """
    target = Path(path)
    with target.open("rb") as fh:
        sample = fh.read(65536)
    _label, codec = _detect_encoding(sample)
    with target.open("r", encoding=codec) as fh:
        for line in fh:
            yield line.rstrip("\n").rstrip("\r")


# ---------------------------------------------------------------
# 圧縮ファイルの透過読み
# ---------------------------------------------------------------
def _archive_kind(path: Path):
    """拡張子から "zip" / "tar" / "gz" / None (素のファイル) を返す。"""
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        return "tar"
    if name.endswith(".gz"):
        return "gz"
    return None


def list_members(path):
    """中身の名前の一覧を返す。

    zip / tar / tar.gz は収まっているファイルの名前 (フォルダは除く)。
    gz 単体は中身を1件として、".gz" を外した名前を返す。
    素のファイルは、そのファイル自身の名前を1件だけ返す。
    """
    target = Path(path)
    kind = _archive_kind(target)
    if kind == "zip":
        with zipfile.ZipFile(target) as zf:
            return [name for name in zf.namelist() if not name.endswith("/")]
    if kind == "tar":
        with tarfile.open(target) as tf:
            return [m.name for m in tf.getmembers() if m.isfile()]
    if kind == "gz":
        return [target.name[: -len(".gz")]]
    return [target.name]


def read_member_text(path, member):
    """list_members で得た1件の中身を、文字コード判定つきで読む。

    (text, encoding) を返す。素のファイルでは member は使わない
    (ファイル自身を読む)。
    """
    target = Path(path)
    kind = _archive_kind(target)
    if kind == "zip":
        with zipfile.ZipFile(target) as zf:
            data = zf.read(member)
    elif kind == "tar":
        with tarfile.open(target) as tf:
            fh = tf.extractfile(member)
            if fh is None:
                raise KeyError(f"tar の中身を取り出せません: {member}")
            data = fh.read()
    elif kind == "gz":
        with gzip.open(target, "rb") as fh:
            data = fh.read()
    else:
        data = target.read_bytes()
    return decode_bytes_auto(data)


# ---------------------------------------------------------------
# 実施フォルダ / アーカイブの洗い出し
# ---------------------------------------------------------------
_ROLE_BY_EXT = {
    "log": "本文", "txt": "本文",
    "csv": "表", "tsv": "表",
    "json": "構造化",
    "zip": "アーカイブ", "gz": "アーカイブ", "tar": "アーカイブ",
    "tgz": "アーカイブ",
    "exe": "バイナリ", "dll": "バイナリ", "bin": "バイナリ",
}


def _ext_of(name: str) -> str:
    suffix = Path(name).suffix
    return suffix[1:].lower() if suffix else ""


def _entry(name, size, ext, modified):
    return {
        "name": name,
        "size": size,
        "ext": ext,
        "modified": modified,
        "role": _ROLE_BY_EXT.get(ext, "不明"),
    }


def _zip_modified(info: zipfile.ZipInfo):
    try:
        return datetime(*info.date_time).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _gz_uncompressed_size(target: Path):
    """gz の末尾4バイト (ISIZE) から、展開後の大きさ (2^32 未満) を読む。"""
    try:
        with target.open("rb") as fh:
            fh.seek(-4, 2)
            tail = fh.read(4)
        if len(tail) == 4:
            return int.from_bytes(tail, "little")
    except OSError:
        pass
    return target.stat().st_size


def inventory(target):
    """対象の中身を洗い出し、{name, size, ext, modified, role} の一覧を返す。

    対象がフォルダなら配下のファイルを再帰的に (名前は相対パス)、
    アーカイブなら収まっているファイルを、素のファイルなら自身を
    1件として返す。modified は epoch 秒 (無ければ None)。
    role は拡張子からの推定 (本文 / 表 / 構造化 / アーカイブ /
    バイナリ / 不明)。
    """
    p = Path(target)
    if p.is_dir():
        entries = []
        for root, dirs, files in os_walk_sorted(p):
            for filename in files:
                fp = Path(root) / filename
                st = fp.stat()
                entries.append(_entry(
                    fp.relative_to(p).as_posix(), st.st_size,
                    _ext_of(filename), st.st_mtime))
        return entries
    kind = _archive_kind(p)
    if kind == "zip":
        with zipfile.ZipFile(p) as zf:
            return [
                _entry(info.filename, info.file_size, _ext_of(info.filename),
                       _zip_modified(info))
                for info in zf.infolist() if not info.is_dir()
            ]
    if kind == "tar":
        with tarfile.open(p) as tf:
            return [
                _entry(m.name, m.size, _ext_of(m.name), m.mtime)
                for m in tf.getmembers() if m.isfile()
            ]
    if kind == "gz":
        inner = p.name[: -len(".gz")]
        st = p.stat()
        return [_entry(inner, _gz_uncompressed_size(p), _ext_of(inner),
                       st.st_mtime)]
    st = p.stat()
    return [_entry(p.name, st.st_size, _ext_of(p.name), st.st_mtime)]


def os_walk_sorted(base: Path):
    """os.walk を、名前順が安定する形で回す小物。"""
    for root, dirs, files in os.walk(base):
        dirs.sort()
        files.sort()
        yield root, dirs, files


# ---------------------------------------------------------------
# 要点の部品
# ---------------------------------------------------------------
def _matcher(pattern):
    """語 (部分一致) か、正規表現 (コンパイル済み) かで照合関数を返す。"""
    if hasattr(pattern, "search"):
        return lambda line: pattern.search(line) is not None
    return lambda line: pattern in line


def _pattern_key(pattern):
    return pattern if isinstance(pattern, str) else pattern.pattern


def count_by_pattern(lines, patterns):
    """パターンごとに、一致する行の件数を数える。

    patterns の要素は、語 (文字列、そのまま含む行を数える) か、
    コンパイル済みの正規表現。戻り値は {パターン文字列: 件数}。
    """
    materialized = list(lines)
    result = {}
    for pattern in patterns:
        match = _matcher(pattern)
        result[_pattern_key(pattern)] = sum(
            1 for line in materialized if match(line))
    return result


def _mask_digits(line):
    """行の中の数字の連続 (ASCII 数字のラン) を '#' に置き換えた形を返す。

    規則：半角数字 0-9 が続く部分は、その長さによらず '#' 1つに置き換わる。
    数字以外の文字はそのまま残る (区切りの '-' や ':'、全角数字も残る)。
    例: "2026-11-14 10:00:06 ERROR 応答 3 回目"
        -> "#-#-# #:#:# ERROR 応答 # 回目"
    日付・時刻も数字のランとして伏せられるので、時刻だけが違う行は
    同じ形になる。
    """
    return re.sub(r"[0-9]+", "#", line)


def top_repeated(lines, n=10, normalize=False):
    """繰り返し出てくる行を、多い順に [(行, 回数), ...] で返す。

    空行は数えない。回数が1回だけの行は「繰り返し」ではないので除く。
    同数のときは、先に出てきた行が先に来る。

    normalize=True のときは、各行を _mask_digits で数字を伏せた形に
    してから集計し、伏せた形の行を返す。実ログは行ごとに時刻や数値が
    変わるため完全一致では頻出行が拾えず、そのようなときに使う。
    """
    if normalize:
        counts = Counter(
            _mask_digits(line) for line in lines if line.strip())
    else:
        counts = Counter(line for line in lines if line.strip())
    return [(line, c) for line, c in counts.most_common() if c >= 2][:n]


def excerpt(lines, pattern, context=2, max_hits=20):
    """パターンに一致する行を、前後の文脈つきで抜き出す。

    戻り値は [{"line_no": 一致行の番号 (1始まり), "lines": [文脈を含む行]}]
    の一覧。pattern は語かコンパイル済みの正規表現。max_hits 件で打ち切る。
    """
    materialized = list(lines)
    match = _matcher(pattern)
    hits = []
    for i, line in enumerate(materialized):
        if match(line):
            start = max(0, i - context)
            end = min(len(materialized), i + context + 1)
            hits.append({"line_no": i + 1, "lines": materialized[start:end]})
            if len(hits) >= max_hits:
                break
    return hits


def head_lines(lines, n=20):
    """先頭 n 行をリストで返す。"""
    result = []
    for line in lines:
        result.append(line)
        if len(result) >= n:
            break
    return result


def tail_lines(lines, n=20):
    """末尾 n 行をリストで返す。"""
    return list(deque(lines, maxlen=n))


# ---------------------------------------------------------------
# 回の比較
# ---------------------------------------------------------------
def diff_lines(text_a, text_b):
    """2つのテキストを、行の集合として比べる。

    (a にだけある行の集合, b にだけある行の集合) を返す。
    同じ行の重複は畳まれる (回数までは見ない)。
    """
    a = set(text_a.splitlines())
    b = set(text_b.splitlines())
    return a - b, b - a
