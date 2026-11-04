# photometa/writer.py
# ExifTool を常駐させて、写真に★（レーティング）とキーワードを書く。
#
# - ExifTool は `-stay_open True -@ -` で1つだけ起こし、引数を1行ずつ標準入力に送って
#   `{readyN}` まで読む（1枚ごとに起動すると 1 枚 0.3 秒以上かかるため）。
#   標準入出力は UTF-8 のバイト列。ファイル名も `-charset filename=utf8` で UTF-8 として渡す。
#   黒いコンソール窓は出さない（CREATE_NO_WINDOW）。
# - `exiftool(-k).exe` という名前（終わりに ENTER を待つ版）でも動く。常駐中は待たないが、
#   終わるときは ENTER 待ちになるので、`-stay_open False` を送ったあと標準入力を閉じる
#   （閉じれば待ちが解ける）。それでも終わらなければ kill する。
# - 書くタグ（エクスプローラー・Lightroom・digiKam で同じ★が見えるように全部そろえる）：
#     XMP-xmp:Rating（-1〜5。正本）
#     XMP-microsoft:RatingPercent / EXIF:RatingPercent（★1=1 ★2=25 ★3=50 ★4=75 ★5=99、0 と -1 は 0）
#     EXIF:Rating（0〜5。EXIF の Rating は符号なしなので -1 は書けない。削除候補は 0）
#   キーワードは XMP-dc:Subject に足す／消すだけ（ほかのキーワードは触らない）。
# - 必ず `-P`（更新日時を保つ。PhotoCuller のキャッシュは「パス＋更新日時」がキー）と
#   `-overwrite_original`（_original の控えを作らない）。`-m` で軽い警告では止めない。
# - EXIF が書けずに失敗したら、XMP のタグだけでもう一度書く（XMP だけでも成功扱い）。
# - RAW は同名のサイドカー（名前.xmp）に書く。無ければ ExifTool が作る。サイドカーには
#   XMP のタグだけを書く。
# - 書いたら変更記録（JOURNAL_PATH、JSON Lines）に1行足す。もう一方のアプリが JournalReader で
#   読んで、開いている一覧に反映する。2MB を超えたら .old に回して新しく始める。
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import config as _config
from . import metacache as _metacache
from .xmp import PhotoMeta, is_photo, is_raw, read_meta, sidecar_path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

JOURNAL_PATH = os.path.join(_config.data_dir(), "photometa_journal.jsonl")
JOURNAL_MAX_BYTES = 2 * 1024 * 1024

RATING_PERCENT = {1: 1, 2: 25, 3: 50, 4: 75, 5: 99}

_COMMON_ARGS = ["-charset", "filename=utf8", "-P", "-overwrite_original", "-m"]
_RE_COUNT = re.compile(r"(\d+) (?:image )?files? (updated|created|unchanged)")


class ExifToolError(Exception):
    """ExifTool が起動できない・応答しない。"""


def rating_percent(rating: int) -> int:
    return RATING_PERCENT.get(rating, 0)


def rating_tags(rating: int, xmp_only: bool = False) -> list[str]:
    """★を書く ExifTool の引数。"""
    pct = rating_percent(rating)
    tags = [f"-XMP-xmp:Rating={rating}", f"-XMP-microsoft:RatingPercent={pct}"]
    if not xmp_only:
        tags += [f"-EXIF:Rating={max(rating, 0)}", f"-EXIF:RatingPercent={pct}"]
    return tags


def keyword_tags(keyword: str, on: bool) -> list[str]:
    """キーワードを足す（on）／消す ExifTool の引数。足すときは先に消して二重に入らないようにする。"""
    tags = [f"-XMP-dc:Subject-={keyword}"]
    if on:
        tags.append(f"-XMP-dc:Subject+={keyword}")
    return tags


def normalize_description(text: str) -> str:
    """メモ（dc:description）の正規化。ExifTool の常駐プロセスへは改行を含む引数を送れない
    （execute が ValueError にする）ので、改行は空白に置き換える。前後の空白も落とす。"""
    return " ".join((text or "").split())


def description_tags(text: str) -> list[str]:
    """メモを書く ExifTool の引数。空ならメモを消す。text は normalize_description 済みのこと。"""
    if not text:
        return ["-XMP-dc:Description="]
    return [f"-XMP-dc:Description={text}"]


def _num(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def edits_tags(edits) -> list[str]:
    """補正（crs:）を書く ExifTool の引数。edits が None なら補正を全部消す。"""
    tags: list[str] = []
    if edits is not None and edits.crop is not None:
        top, left, bottom, right = edits.crop
        tags.append("-XMP-crs:HasCrop=True")
        tags += [f"-XMP-crs:CropTop={_num(top)}", f"-XMP-crs:CropLeft={_num(left)}",
                 f"-XMP-crs:CropBottom={_num(bottom)}", f"-XMP-crs:CropRight={_num(right)}",
                 f"-XMP-crs:CropAngle={_num(edits.angle)}"]
    elif edits is not None and edits.angle:
        tags.append(f"-XMP-crs:CropAngle={_num(edits.angle)}")
    else:
        tags.append("-XMP-crs:HasCrop=False")
        tags += [f"-XMP-crs:{name}=" for name in
                 ("CropTop", "CropLeft", "CropBottom", "CropRight", "CropAngle")]
    if edits is None:
        tags += [f"-XMP-crs:{name}=" for name in
                 ("Exposure2012", "Contrast2012", "Saturation",
                  "IncrementalTemperature", "IncrementalTint")]
    else:
        tags += [f"-XMP-crs:Exposure2012={_num(edits.exposure)}",
                 f"-XMP-crs:Contrast2012={_num(edits.contrast)}",
                 f"-XMP-crs:Saturation={_num(edits.saturation)}",
                 # 色温度・色合いは相対値の形（Lightroom が JPEG などに使うもの）で書く。
                 # 以前の "Temperature"/"TemperatureUnit" は ExifTool に無い名前で、
                 # -m のせいで警告だけで捨てられていた（2026-11-04）
                 f"-XMP-crs:IncrementalTemperature={_num(edits.temperature)}",
                 f"-XMP-crs:IncrementalTint={_num(edits.tint)}"]
    # 以前の版が色合いを書いていた crs:Tint は、読み取りの予備になるので消しておく
    # （残すと、補正を消した・0 にしたあとも古い色合いが読まれる）
    tags.append("-XMP-crs:Tint=")
    return tags


@dataclass
class WriteResult:
    path: str                      # 指定された写真のパス（RAW なら RAW 本体）
    ok: bool
    error: str | None = None       # 失敗の理由（ExifTool の Error 行など）
    before: PhotoMeta | None = None   # 書く前の★・キーワード（取り消しに使う。読めなければ None）
    after: PhotoMeta | None = None    # 書いた後に読み直した値
    note: str | None = None        # 補足（「EXIF は書けず XMP だけ書いた」など）
    op: tuple = field(default=())  # ("rating", 3) / ("keyword", "みてね済", True)


def _parse_output(out: str) -> tuple[bool, str | None]:
    """ExifTool の出力から、成功したか・失敗の理由を読む。"""
    errors = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("Error")]
    done = sum(int(m.group(1)) for m in _RE_COUNT.finditer(out))
    if done >= 1 and not errors:
        return True, None
    if errors:
        return False, errors[0]
    text = " / ".join(ln.strip() for ln in out.splitlines() if ln.strip())
    return False, text or "ExifTool から結果が返りませんでした"


class ExifToolSession:
    """常駐させた ExifTool 1つ。スレッドセーフ（書き込みは Lock で1つずつ）。

    with ExifToolSession(path) as et:
        results = et.set_rating(paths, 5)
    """

    def __init__(self, exe: str, timeout: float = 60.0, journal_path: str | None = None,
                 popen=subprocess.Popen, metacache=None):
        self.exe = exe
        self.timeout = timeout
        self.journal_path = journal_path
        # ★の読み取りキャッシュ（metacache.MetaCache）。書き込みは更新日時を元に戻すので、
        # 成功したらキャッシュも新しい値に直す。None なら触らない（open_session は
        # 既定のキャッシュを渡す。直接作るテスト等は実キャッシュを汚さない）。
        self._metacache = metacache
        self._popen = popen
        self._proc = None
        self._out: queue.Queue | None = None
        self._lock = threading.RLock()
        self._seq = 0

    # ------------------------------------------------------------ 起動・終了

    def _start(self):
        try:
            self._proc = self._popen(
                [self.exe, "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            self._proc = None
            raise ExifToolError(f"ExifTool を起動できません: {e}") from e
        self._out = queue.Queue()
        threading.Thread(target=self._reader, args=(self._proc, self._out),
                         name="photometa-exiftool-reader", daemon=True).start()

    @staticmethod
    def _reader(proc, q):
        try:
            while True:
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") \
                    else proc.stdout.read(1)
                if not chunk:
                    break
                q.put(chunk)
        except (OSError, ValueError):
            pass
        q.put(None)

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self, timeout: float = 5.0):
        """ExifTool を終わらせる。応答しなければ kill。"""
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    proc.stdin.write(b"-stay_open\nFalse\n")
                    proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.stdin.close()      # (-k) 版の ENTER 待ちを解く
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout)
                except (OSError, subprocess.SubprocessError):
                    pass
            try:
                proc.stdout.close()     # 閉じないと ResourceWarning（unclosed file）が出る
            except (OSError, ValueError):
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 実行

    def execute(self, args: list[str]) -> str:
        """引数のリストで ExifTool を1回動かし、出力（{ready} の前まで）を返す。"""
        for a in args:
            if "\n" in a or "\r" in a:
                raise ValueError("ExifTool の引数に改行は使えません")
        with self._lock:
            if not self.alive():
                self.close(timeout=1.0)
                self._start()
            self._seq += 1
            marker = f"{{ready{self._seq}}}".encode()
            payload = "\n".join(args + [f"-execute{self._seq}"]) + "\n"
            try:
                self._proc.stdin.write(payload.encode("utf-8"))
                self._proc.stdin.flush()
            except (OSError, ValueError) as e:
                self.close(timeout=1.0)
                raise ExifToolError(f"ExifTool に送れません: {e}") from e
            buf = b""
            deadline = time.monotonic() + self.timeout
            while True:
                idx = buf.find(marker)
                if idx >= 0:
                    return buf[:idx].decode("utf-8", errors="replace")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close(timeout=1.0)
                    raise ExifToolError("ExifTool が応答しません（時間切れ）")
                try:
                    chunk = self._out.get(timeout=remaining)
                except queue.Empty:
                    continue
                if chunk is None:
                    self.close(timeout=1.0)
                    raise ExifToolError("ExifTool が途中で終了しました")
                buf += chunk

    def version(self) -> str:
        return self.execute(["-ver"]).strip()

    # ------------------------------------------------------------ 書き込み

    def _write_one(self, path: str, op: tuple, tags_for) -> WriteResult:
        """1枚書く。tags_for(xmp_only) が書くタグの引数を返す。"""
        if not is_photo(path):
            return WriteResult(path, False, "写真ではありません", op=op)
        if not os.path.isfile(path):
            return WriteResult(path, False, "ファイルがありません", op=op)
        raw = is_raw(path)
        target = sidecar_path(path) if raw else path
        before = read_meta(path)
        try:
            st = os.stat(target)
        except OSError:
            st = None          # サイドカーがまだ無い（ExifTool が作る）
        if st is not None and not os.access(target, os.W_OK):
            # ExifTool に任せると、置き換えに失敗して「名前_exiftool_tmp」を写真の隣に残す
            return WriteResult(path, False, "読み取り専用のファイルです", before=before, op=op)
        tmp = target + "_exiftool_tmp"
        tmp_existed = os.path.exists(tmp)
        note = None
        try:
            ok, err = _parse_output(self.execute(_COMMON_ARGS + tags_for(raw) + [target]))
            if not ok and not raw:
                ok2, err2 = _parse_output(self.execute(_COMMON_ARGS + tags_for(True) + [target]))
                if ok2:
                    ok, err, note = True, None, f"EXIF は書けなかったので XMP だけ書きました（{err}）"
        except ExifToolError as e:
            return WriteResult(path, False, str(e), before=before, op=op)
        finally:
            if not tmp_existed and os.path.exists(tmp):
                try:
                    os.remove(tmp)      # 失敗したときに ExifTool が残す一時ファイル
                except OSError:
                    pass
        if ok and st is not None:
            # -P でも更新日時は Perl の浮動小数を通るので、1マイクロ秒ほどずれる
            # （1700000000.1234567 → .1234544 を実測）。PhotoCuller のキャッシュは st_mtime の
            # 文字列がキーなので、そのずれでも作り直しになる。書く前の値（100ns 単位）に戻す。
            try:
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                pass
        after = read_meta(path) if ok else None
        if ok and self._metacache is not None:
            try:
                self._metacache.update(path, after)
            except Exception:  # noqa: BLE001  キャッシュを直せなくても書き込みは成功
                pass
        result = WriteResult(path, ok, err, before=before, after=after, note=note, op=op)
        if ok:
            self._journal(result)
        return result

    def set_rating(self, paths, rating: int) -> list[WriteResult]:
        """★を書く（-1〜5）。1枚ごとの結果（変更前の値つき）を返す。"""
        if isinstance(rating, bool) or not isinstance(rating, int) or not -1 <= rating <= 5:
            raise ValueError(f"★は -1〜5 の整数です: {rating!r}")
        with self._lock:
            return [self._write_one(os.fspath(p), ("rating", rating),
                                    lambda xmp_only: rating_tags(rating, xmp_only))
                    for p in paths]

    def set_keyword(self, paths, keyword: str, on: bool) -> list[WriteResult]:
        """キーワードを足す（on=True）／外す。1枚ごとの結果を返す。"""
        keyword = (keyword or "").strip()
        if not keyword:
            raise ValueError("キーワードが空です")
        with self._lock:
            return [self._write_one(os.fspath(p), ("keyword", keyword, bool(on)),
                                    lambda xmp_only: keyword_tags(keyword, on))
                    for p in paths]

    def set_edits(self, paths, edits) -> list[WriteResult]:
        """補正（crs:）を書く。edits が None なら補正を全部消す。"""
        if edits is not None and edits.is_empty():
            edits = None
        with self._lock:
            return [self._write_one(os.fspath(p), ("edits", edits),
                                    lambda xmp_only: edits_tags(edits))
                    for p in paths]

    def set_description(self, paths, text: str) -> list[WriteResult]:
        """メモ（dc:description）を書く。空文字ならメモを消す。改行は空白に置き換わる。"""
        text = normalize_description(text)
        with self._lock:
            return [self._write_one(os.fspath(p), ("description", text),
                                    lambda xmp_only: description_tags(text))
                    for p in paths]

    def restore(self, results: list[WriteResult]) -> list[WriteResult]:
        """取り消し（Z）：set_rating / set_keyword の結果から、書く前の値に戻す。

        成功したもの・変更前の値が読めていたものだけを戻す。同じ値はまとめて書く。
        """
        out: list[WriteResult] = []
        groups: dict[tuple, list[str]] = {}
        for r in results:
            if not r.ok or r.before is None or not r.op:
                continue
            if r.op[0] == "rating":
                groups.setdefault(("rating", r.before.rating), []).append(r.path)
            elif r.op[0] == "keyword":
                kw = r.op[1]
                had = any(k.casefold() == kw.casefold() for k in r.before.keywords)
                groups.setdefault(("keyword", kw, had), []).append(r.path)
            elif r.op[0] == "edits":
                groups.setdefault(("edits", r.before.edits), []).append(r.path)
            elif r.op[0] == "description":
                groups.setdefault(("description", r.before.description), []).append(r.path)
        for key, paths in groups.items():
            if key[0] == "rating":
                out += self.set_rating(paths, key[1])
            elif key[0] == "edits":
                out += self.set_edits(paths, key[1])
            elif key[0] == "description":
                out += self.set_description(paths, key[1])
            else:
                out += self.set_keyword(paths, key[1], key[2])
        return out

    # ------------------------------------------------------------ 変更記録

    def _journal(self, r: WriteResult):
        meta = r.after
        if meta is None:
            return
        append_journal(r.path, meta.rating, meta.keywords, self.journal_path,
                       edits=meta.edits, description=meta.description)


def append_journal(path: str, rating: int, keywords, journal_path: str | None = None,
                   edits=None, description: str = "") -> None:
    """変更記録に1行足す（書けなくても止めない）。2MB を超えていたら .old に回してから書く。"""
    jp = journal_path or JOURNAL_PATH
    entry = {
        "path": os.path.abspath(path),
        "rating": rating,
        "keywords": list(keywords),
        "time": round(time.time(), 3),
        "pid": os.getpid(),
        "description": description or "",
    }
    if edits is not None:
        entry["edits"] = {
            "crop": list(edits.crop) if edits.crop is not None else None,
            "angle": edits.angle, "exposure": edits.exposure,
            "contrast": edits.contrast, "saturation": edits.saturation,
            "temperature": edits.temperature, "tint": edits.tint,
        }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        os.makedirs(os.path.dirname(jp) or ".", exist_ok=True)
        try:
            if os.path.getsize(jp) > JOURNAL_MAX_BYTES:
                os.replace(jp, jp + ".old")
        except OSError:
            pass
        with open(jp, "ab") as f:
            f.write(line.encode("utf-8"))
    except OSError:
        pass


class JournalReader:
    """変更記録を前回の位置から読む。

    reader = JournalReader()          # 今の末尾から（これより前の記録は読まない）
    for e in reader.read_new(): ...   # e = {"path", "rating", "keywords", "time", "pid"}

    記録が .old に回された（ファイルが替わった・縮んだ）ら先頭から読み直す。
    行の途中までしか書かれていないものは次回に回す。
    """

    def __init__(self, path: str | None = None, from_start: bool = False):
        self.path = path or JOURNAL_PATH
        self._pos = 0
        self._ident = None
        if not from_start:
            st = self._stat()
            if st is not None:
                self._pos, self._ident = st.st_size, self._identity(st)

    def _stat(self):
        try:
            return os.stat(self.path)
        except OSError:
            return None

    @staticmethod
    def _identity(st):
        return (st.st_ino, st.st_dev) if st.st_ino else None

    def read_new(self, exclude_pid: int | None = None) -> list[dict]:
        st = self._stat()
        if st is None:
            self._pos, self._ident = 0, None
            return []
        ident = self._identity(st)
        if st.st_size < self._pos or (ident is not None and self._ident is not None
                                      and ident != self._ident):
            self._pos = 0
        self._ident = ident
        if st.st_size == self._pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                data = f.read(st.st_size - self._pos)
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self._pos += end + 1
        out = []
        for raw in data[:end].split(b"\n"):
            try:
                e = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(e, dict) or not isinstance(e.get("path"), str):
                continue
            if exclude_pid is not None and e.get("pid") == exclude_pid:
                continue
            out.append(e)
        return out


def open_session(config: dict | None = None, **kwargs) -> ExifToolSession | None:
    """設定（無ければ共通設定ファイル）から ExifTool を探してセッションを作る。見つからなければ None。

    書き込みの成功時に★の読み取りキャッシュも直すよう、既定のキャッシュを渡す
    （metacache=None を明示すれば無効にできる）。
    """
    cfg = config if config is not None else _config.load_config()
    exe = _config.find_exiftool(cfg)
    if not exe:
        return None
    kwargs.setdefault("metacache", _metacache.default_cache())
    return ExifToolSession(exe, **kwargs)
