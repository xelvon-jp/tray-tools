# filer_everything.py
# あっふ(filer.py)の Everything 検索(Shift+F)の、Qt に依存しない部分。
#
# Everything 本体には問い合わせの口(IPC)があり、コマンドライン版の es.exe がそれを
# 叩いて結果を返してくれる。ここでは es.exe を子プロセスとして起こし、結果を
# ファイラの一覧の項目(filer_core.Entry)にするところまでを受け持つ。
#
# 文字コードについて(いちばん踏みやすいところ)
#   es.exe の標準出力はコンソールのコードページ(日本語環境では cp932)で書かれ、
#   cp932 にない字(絵文字・一部の漢字)を含むパスは ? に化ける。パスが化けると、
#   そのファイルはコピーも削除もできない(別のファイルを指すことすらありうる)。
#   そこで標準出力は使わず、-export-txt で一時ファイルへ書かせ、-utf8-bom で
#   UTF-8(BOM付き)に固定して読む。BOM が付いていれば「UTF-8 で書かれた」ことが
#   確かめられる(付いていなければ念のため cp932 として読み直す)。
#
# 引数の渡し方
#   subprocess には引数のリストで渡す(shell は通さない)。検索語は空白で語に分けて
#   1語1引数で渡す(Everything の AND 検索になる)。"..." で囲んだ語は1つの引数に
#   まとめる(subprocess が引用符で囲むので、es.exe には語句として届く)。
#   - や / で始まる語は es.exe がスイッチと取り違えるので、*語* にして渡す
#   (Everything ではワイルドカード付きの語は名前全体に対して照合されるので、
#   *語* は「語を含む」と同じ意味になる)。
import os
import re
import stat
import subprocess
import tempfile
import time

import filer_core as core

ES_TOOL_NAME = "es"            # settings.json の filer.tools で場所を指定するときの名前
ES_LIMIT = 1000                # 結果の上限(超えたら打ち切って知らせる)
ES_TIMEOUT = 10.0              # es.exe の応答を待つ秒数
ERROR_NOT_RUNNING = 8          # es.exe の終了コード: Everything が起動していない(IPC が無い)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# es.exe の終了コード(es.exe -h の Error levels / voidtools のドキュメント)。
ES_ERRORS = {
    1: "es.exe の初期化に失敗しました（ウィンドウクラスの登録）",
    2: "es.exe の初期化に失敗しました（受信用ウィンドウ）",
    3: "メモリが足りません",
    4: "es.exe の引数が足りません",
    5: "結果の書き出し先を作れません",
    6: "es.exe が知らないオプションです（es.exe が古い可能性があります）",
    7: "Everything への問い合わせに失敗しました",
    ERROR_NOT_RUNNING: "Everything が起動していません（Everything を起動してからもう一度どうぞ）",
}

NOT_FOUND_MESSAGE = ("es.exe が見つかりません。Everything と es.exe（コマンドライン版）を入れるか、"
                     "settings.json の filer.tools に \"es\": \"es.exe の場所\" を書いてください")


def default_locations(environ=None) -> list:
    """es.exe を置きがちな場所(Everything のインストール先に同梱されることが多い)。"""
    environ = os.environ if environ is None else environ
    found = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = environ.get(var)
        if base:
            found.append(os.path.join(base, "Everything", "es.exe"))
    local = environ.get("LOCALAPPDATA")
    if local:
        found.append(os.path.join(local, "Programs", "Everything", "es.exe"))
    unique = []
    for path in found:
        if os.path.normcase(path) not in {os.path.normcase(p) for p in unique}:
            unique.append(path)
    return unique


def find_es(resolver, environ=None, exists=os.path.isfile):
    """es.exe を起こすときの先頭の引数列([exe, 追加の引数…])。見つからなければ None。

    1. settings.json の filer.tools の "es"(PC ごとの場所。"-instance 1.5a" のような
       追加の引数もここに書ける)
    2. App Paths と PATH("es.exe")
    3. 既定のインストール先(Program Files\\Everything\\es.exe など)"""
    try:
        spec = resolver.tool(ES_TOOL_NAME) if resolver is not None else None
    except OSError:
        spec = None
    if spec:
        return list(spec[0])
    for path in default_locations(environ):
        try:
            if exists(path):
                return [path]
        except OSError:
            continue
    return None


_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')


def split_query(text: str) -> list:
    """検索語を es.exe に渡す引数の並びにする(説明は冒頭の「引数の渡し方」)。"""
    words = []
    for quoted, plain in _TOKEN_RE.findall(text or ""):
        word = quoted if quoted else plain.replace('"', "")
        if not word.strip():
            continue
        if word[0] in "-/" and " " not in word:
            word = f"*{word}*"
        words.append(word)
    return words


def search_root(path: str) -> str:
    """-path に渡すフォルダ。ドライブ直下以外は末尾の \\ を落とす(引用符の直前の \\ は
    コマンドラインの解釈が揺れるため)。"""
    path = core.normalize_dir(path)
    return path


def build_args(es_argv, words, root, limit: int, out_path: str) -> list:
    """es.exe に渡す引数のリスト。上限の判定のため limit+1 件まで取る。"""
    args = list(es_argv) + ["-n", str(int(limit) + 1), "-export-txt", out_path, "-utf8-bom"]
    if root:
        args += ["-path", search_root(root)]
    return args + list(words)


def decode_export(data: bytes) -> str:
    """-export-txt の中身を文字列に。BOM があれば UTF-8、無ければ UTF-8 → cp932 の順。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace")


def parse_export(data: bytes) -> list:
    """書き出された結果をパスの一覧に。フォルダは末尾に \\ が付いてくるので落とす。"""
    paths = []
    for line in decode_export(data).splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        paths.append(core.normalize_dir(line) if core.is_root(line) else line.rstrip("\\/"))
    return paths


def _decode_console(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace").strip()


def error_message(code: int, stderr: bytes = b"") -> str:
    text = ES_ERRORS.get(code, f"es.exe が失敗しました（終了コード {code}）")
    detail = _decode_console(stderr)
    if detail and code not in ES_ERRORS:
        text += f"  {detail.splitlines()[0]}"
    return text


class EsResult:
    """run_search の結果。status は "done" / "limit" / "error" / "timeout" / "cancel"。"""

    def __init__(self, status: str, paths=None, message: str = ""):
        self.status = status
        self.paths = paths or []
        self.message = message


def run_search(es_argv, query: str, root=None, limit: int = ES_LIMIT,
               timeout: float = ES_TIMEOUT, cancel=None, popen=subprocess.Popen,
               temp_dir=None) -> EsResult:
    """es.exe で探す。読み取りスレッドから呼ぶ(終わるまで戻らない)。

    root を渡すとそのフォルダより下だけ、None なら全体。結果は limit 件まで
    (それより多ければ status="limit")。cancel(threading.Event)が立つか timeout 秒を
    過ぎたら es.exe を止める。黒いコンソール窓は出さない(CREATE_NO_WINDOW)。"""
    words = split_query(query)
    if not words:
        return EsResult("error", message="検索語が空です")
    fd, out_path = tempfile.mkstemp(prefix="affx_es_", suffix=".txt", dir=temp_dir)
    os.close(fd)
    try:
        args = build_args(es_argv, words, root, limit, out_path)
        try:
            proc = popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            return EsResult("error", message=f"es.exe を起動できません（{e}）")
        deadline = time.monotonic() + timeout
        while True:
            try:
                _stdout, stderr = proc.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                stopped = None
                if cancel is not None and cancel.is_set():
                    stopped = EsResult("cancel")
                elif time.monotonic() > deadline:
                    stopped = EsResult("timeout", message=f"es.exe が {timeout:g}秒で応答しません")
                if stopped is not None:
                    try:
                        proc.kill()
                        proc.communicate(timeout=2)
                    except (OSError, subprocess.SubprocessError):
                        pass
                    return stopped
        if proc.returncode != 0:
            return EsResult("error", message=error_message(proc.returncode, stderr))
        try:
            with open(out_path, "rb") as f:
                data = f.read()
        except OSError as e:
            return EsResult("error", message=f"es.exe の結果を読めません（{e}）")
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    paths = parse_export(data)
    if len(paths) > limit:
        return EsResult("limit", paths[:limit])
    return EsResult("done", paths)


def to_entries(paths, root=None, stat_func=os.stat):
    """es.exe の結果(フルパス)を一覧の項目にする。(項目, 無くなっていた件数)。

    - 名前は root より下なら root からの相対パス(フラット表示・文字列検索と同じ形)、
      それ以外(全体から探したとき)はフルパス(一時置き場と同じ形)。どちらでも窓側の
      os.path.join(窓のパス, 名前) が実パスになる
    - 属性・サイズ・日時はその場で stat し直す(Everything の索引は少し遅れることが
      あり、属性は索引に無いことも多い)。消えていたものは落とす
    - 隠し・システムの絞り込みはここではしない(通常のフォルダと同じく窓の側で
      Shift+Z の設定を掛ける。あとから Shift+Z を切り替えても出し入れできるように)"""
    entries, missing = [], 0
    base = core.normalize_dir(root) if root else None
    seen = set()
    for path in paths:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            st = stat_func(path, follow_symlinks=False)
        except OSError:
            missing += 1
            continue
        attrs = getattr(st, "st_file_attributes", 0)
        is_dir = bool(attrs & stat.FILE_ATTRIBUTE_DIRECTORY) or stat.S_ISDIR(st.st_mode)
        name = path
        if base is not None:
            try:
                rel = os.path.relpath(path, base)
            except ValueError:   # 別のドライブ
                rel = None
            if rel and rel != "." and not rel.startswith(".."):
                name = rel
        entries.append(core.Entry(name, is_dir, 0 if is_dir else st.st_size,
                                  st.st_mtime, attrs))
    return entries, missing
