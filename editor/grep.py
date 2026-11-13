# editor/grep.py
# フォルダ内の横断検索(grep)の純粋部分(Qt に依存しない)。
#
# 重くならないための決め:
#   - 対象は「テキストらしい拡張子」のファイルだけ(加えて拡張子なし)。
#     バイナリや依存フォルダ(.git や node_modules 等)は歩かない
#   - 1ファイルの大きさに上限を超えたものは読まない
#   - 結果の件数に上限を付け、達したら打ち切って truncated で知らせる
#   - 文字コードは text_encoding の判定に合わせ、読めないバイトは
#     置換文字で読む(ビュアや文書の読み込みと同じ倒し方)
# 一致は行単位。検索語は文字どおり(正規表現は使わない)に探す。
import os
from dataclasses import dataclass, field

import text_encoding
from editor import document as docmod

# 対象にする拡張子(小文字、ドット付き)。テキストの中身を持つことが
# 分かっているものだけに絞る。
DEFAULT_EXTENSIONS = frozenset({
    ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".ini", ".cfg",
    ".xml", ".html", ".htm", ".css", ".js", ".ts", ".py", ".rb",
    ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs", ".sh",
    ".bat", ".ps1", ".yaml", ".yml", ".toml",
})

# 歩かないフォルダ(依存物や履歴。中を検索しても意味が薄く、重い)。
SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", "__pycache__", "node_modules",
    ".venv", "venv", ".idea", ".vscode",
})

MAX_FILE_BYTES = 2 * 1024 * 1024   # これより大きいファイルは読まない
MAX_RESULTS = 1000                 # 一致の上限(達したら打ち切り)
PROGRESS_EVERY = 20                # 進捗を知らせる間隔(ファイル数)


@dataclass
class GrepMatch:
    path: str
    line: int      # 1始まりの行番号
    text: str      # その行の内容(改行なし)


@dataclass
class GrepResult:
    matches: list = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False   # 件数の上限で打ち切ったか


def is_target_file(path, extensions=None, max_bytes=MAX_FILE_BYTES):
    """検索の対象にするファイルか(拡張子と大きさ)。"""
    ext = os.path.splitext(path)[1].lower()
    targets = DEFAULT_EXTENSIONS if extensions is None else extensions
    if ext and ext not in targets:
        return False
    try:
        return os.path.getsize(path) <= max_bytes
    except OSError:
        return False


def iter_target_files(root, extensions=None, max_bytes=MAX_FILE_BYTES):
    """root の下の対象ファイルを、決まった順(パス順)で列挙する。"""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if is_target_file(path, extensions, max_bytes):
                found.append(path)
    return found


def read_lines(path):
    """ファイルを行の並びとして読む。文字コードは text_encoding の
    判定に合わせ、改行は \n にそろえる。読めなければ空の並び。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    encoding, start = text_encoding.detect_encoding(
        data[:docmod.DETECT_HEAD_BYTES])
    text = data[start:].decode(encoding, errors="replace")
    return text.replace("\r\n", "\n").split("\n")


def grep_folder(root, word, *, case_sensitive=False, extensions=None,
                max_bytes=MAX_FILE_BYTES, max_results=MAX_RESULTS,
                progress=None):
    """root の下を word で横断検索し、GrepResult を返す。

    progress は (調べたファイル数, いま調べているパス) を受ける関数で、
    PROGRESS_EVERY 件ごとに呼ばれる(窓の進捗表示用。例外を上げても
    検索は止めない)。"""
    result = GrepResult()
    if not word or not root or not os.path.isdir(root):
        return result
    needle = word if case_sensitive else word.casefold()
    for path in iter_target_files(root, extensions, max_bytes):
        result.files_scanned += 1
        if progress is not None and result.files_scanned % PROGRESS_EVERY == 1:
            try:
                progress(result.files_scanned, path)
            except Exception:  # noqa: BLE001  進捗の失敗で検索を止めない
                pass
        for number, line in enumerate(read_lines(path), start=1):
            haystack = line if case_sensitive else line.casefold()
            if needle in haystack:
                result.matches.append(GrepMatch(path, number, line))
                if len(result.matches) >= max_results:
                    result.truncated = True
                    return result
    return result
