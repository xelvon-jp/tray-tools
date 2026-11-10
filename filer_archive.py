# filer_archive.py
# あっふの「書庫の中を見る」部分(あふｗの仮想フォルダに当たるもの)。Qt を使わない。
#
# ZIP は標準の zipfile、7z は py7zr、tar 系は標準の tarfile で読む。py7zr と
# pyzipper(AES 形式の ZIP 用)は書庫を開くときに初めて import する
# (ファイラの起動を重くしない・入っていない PC でも ZIP と tar は使える)。
#
# パスの形
#   書庫の中は「書庫ファイルのパス＋中のパス」で表す。D:\x\foo.zip\sub のように、
#   ふだんのフォルダのパスと同じ書き方で履歴・O・H・前回の復元をそのまま通す。
#   中のパス(inner)は "sub/deeper" のように / 区切り、書庫のルートは ""。
#
# 安全
#   中のパスに .. や絶対パス・ドライブ名を含むエントリ(zip slip)は一覧にも出さず、
#   展開もしない(build_index で除き、展開の直前にも safe_relpath でもう一度確かめる)。
import os
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass

import filer_core as core

TAR_EXTS = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")

ARCHIVE_EXTS = (".zip", ".7z") + TAR_EXTS


def archive_kind(path: str) -> str:
    """"zip" / "7z" / "tar" のどれで読むか(拡張子で決める)。"""
    lower = path.lower()
    if lower.endswith(".7z"):
        return "7z"
    if lower.endswith(TAR_EXTS):
        return "tar"
    return "zip"


def archive_stem(name: str) -> str:
    """書庫のファイル名から拡張子を除いた名前(.tar.gz のような複合もまとめて除く)。
    U の展開で「書庫名のフォルダ」を作るときに使う。"""
    lower = name.lower()
    for ext in sorted(ARCHIVE_EXTS, key=len, reverse=True):
        if lower.endswith(ext):
            return name[: -len(ext)] or name
    return name

# 書庫の一覧は大きいと時間がかかり、ネットワーク上の書庫もありうるので見切りを長めに。
ARCHIVE_LIST_TIMEOUT = 30.0

# 中のファイルを開く(Enter/E)ときの一時フォルダの置き場所。
TEMP_ROOT = os.path.join(tempfile.gettempdir(), "traytools-filer")

_COPY_CHUNK = 1024 * 1024
# ZIP の展開で書きかけを置く名前(展開先の隣)。書き終えてから本来の名前へ置き換える。
PART_SUFFIX = ".filer-part"


class ArchiveError(Exception):
    """書庫として読めない・未対応の形式など。メッセージはそのまま利用者に見せる。"""


class PasswordRequired(ArchiveError):
    pass


def is_archive_name(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_EXTS)


def looks_like_archive_path(path: str) -> bool:
    """字面だけで「書庫の中(か書庫そのもの)らしい」か。I/O をしない(見切り時間の決定に使う)。"""
    if not path:
        return False
    return any(is_archive_name(part) for part in os.path.normpath(path).split(os.sep))


def split_archive_path(path: str, isfile=os.path.isfile):
    """path を (書庫ファイル, 中のパス) に分ける。書庫が絡まないパスなら None。

    書庫の拡張子を持つ部分だけ isfile で確かめる(.zip という名前のフォルダもありうる)。
    浅い方から見るので、書庫の中にある書庫は「外側の書庫の中のファイル」として扱う。"""
    if not path:
        return None
    norm = os.path.normpath(path)
    drive, rest = os.path.splitdrive(norm)
    parts = [p for p in rest.split(os.sep) if p]
    current = drive + os.sep
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        if is_archive_name(part):
            try:
                if isfile(current):
                    return current, "/".join(parts[index + 1:])
            except OSError:
                return None
    return None


def display_path(archive: str, inner: str) -> str:
    """一覧のパスとして見せる・履歴に積む形。"""
    if not inner:
        return archive
    return os.path.join(archive, inner.replace("/", os.sep))


def join_inner(inner: str, name: str) -> str:
    return f"{inner}/{name}" if inner else name


def safe_relpath(name: str):
    """書庫の中のパスを安全な相対パス("a/b")にする。危ないもの・空なら None。

    \\ も区切りとして扱う(Windows で作られた ZIP には \\ のものがある)。"""
    if not name:
        return None
    text = name.replace("\\", "/")
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        return None
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or ":" in part:
            return None
        parts.append(part)
    return "/".join(parts) or None


def decode_zip_name(info) -> str:
    """ZIP のファイル名。UTF-8 の印(フラグ 0x800)が無ければ cp932 として読み直す。

    zipfile は印の無い名前を cp437 で読むので、日本の ZIP は文字化けする。cp437 に戻して
    cp932 で読み直し、だめなら(本当に cp437 だった等)元のまま。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


@dataclass
class Member:
    source: str          # 書庫の中での本来の名前(zipfile / py7zr に渡す名前)。合成フォルダは ""
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    encrypted: bool = False
    attrs: int = 0       # DOS 属性(隠し・システム・読み込み専用)。取れなければ 0
    compress_type: int = 0  # ZIP の圧縮種別(99 は WinZip AES。zipfile では開けない)


# ZIP の external_attr の下位バイトに入る DOS 属性のうち、表示に使うもの。
_DOS_ATTR_MASK = 0x01 | 0x02 | 0x04  # 読み込み専用 / 隠し / システム


def zip_dos_attrs(info) -> int:
    """ZIP のエントリの DOS 属性(読み込み専用・隠し・システム)。取れなければ 0。

    external_attr の下位16ビットは、Windows(MS-DOS 互換)で作った ZIP なら DOS 属性。
    Unix で作った ZIP は上位16ビットがモードで、下位は 0 か意味の無い値なので見ない
    (create_system 3 = Unix。誤って「隠し」と読んで消えて見えるより、出す方がよい)。"""
    if getattr(info, "create_system", 0) == 3:
        return 0
    return int(getattr(info, "external_attr", 0)) & _DOS_ATTR_MASK


class ArchiveIndex:
    """書庫1つの中身。dirs は {中のフォルダ: {名前(casefold): Entry}}、members は
    {中のパス: Member}。ディレクトリのエントリが無い ZIP でも中間フォルダを合成する。"""

    def __init__(self, path: str, kind: str):
        self.path = path
        self.kind = kind
        self.dirs = {"": {}}
        self.members = {}
        self.skipped = 0             # 危ないパスとして除いた数
        self.needs_password = False  # 7z: 中身が暗号化されている

    def add(self, inner: str, member: Member) -> None:
        parts = inner.split("/")
        # 中間フォルダを合成する(既にあれば触らない)。
        for depth in range(1, len(parts)):
            folder = "/".join(parts[:depth])
            if folder not in self.members:
                self.members[folder] = Member("", True)
                parent = "/".join(parts[:depth - 1])
                self.dirs.setdefault(parent, {})[parts[depth - 1].casefold()] = core.Entry(
                    parts[depth - 1], True)
            self.dirs.setdefault(folder, {})
        name = parts[-1]
        parent = "/".join(parts[:-1])
        existing = self.members.get(inner)
        if existing is not None and existing.is_dir and member.is_dir and not existing.source:
            existing.source = member.source  # 合成していたフォルダに実体が来た
            existing.mtime = member.mtime
        else:
            self.members[inner] = member
        self.dirs.setdefault(parent, {})[name.casefold()] = core.Entry(
            name, member.is_dir, 0 if member.is_dir else member.size, member.mtime,
            member.attrs)
        if member.is_dir:
            self.dirs.setdefault(inner, {})

    def has_dir(self, inner: str) -> bool:
        return inner in self.dirs

    def list_dir(self, inner: str) -> list:
        return list(self.dirs.get(inner, {}).values())

    def descendants(self, inner: str) -> list:
        """inner の下にあるすべての中のパス(inner 自身は含めない)。"""
        prefix = inner + "/"
        return [p for p in self.members if p.startswith(prefix)]


def _zip_mtime(info) -> float:
    try:
        return time.mktime(info.date_time + (0, 0, -1))
    except (OverflowError, ValueError):
        return 0.0


def _build_zip(path: str) -> ArchiveIndex:
    index = ArchiveIndex(path, "zip")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ArchiveError(f"ZIP として読めません: {e}") from e
    with zf:
        for info in zf.infolist():
            inner = safe_relpath(decode_zip_name(info))
            if inner is None:
                index.skipped += 1
                continue
            index.add(inner, Member(info.filename, info.is_dir(), info.file_size,
                                    _zip_mtime(info), bool(info.flag_bits & 0x1),
                                    zip_dos_attrs(info), info.compress_type))
    return index


def _import_py7zr():
    try:
        import py7zr  # noqa: PLC0415  書庫を開くときだけ読む
    except ImportError as e:
        raise ArchiveError("7z を開くには py7zr が必要です（pip install py7zr）") from e
    return py7zr


def _build_7z(path: str, password: str = None) -> ArchiveIndex:
    py7zr = _import_py7zr()
    index = ArchiveIndex(path, "7z")
    try:
        with py7zr.SevenZipFile(path, "r", password=password or None) as z:
            index.needs_password = bool(z.needs_password())
            infos = z.list()
    except py7zr.exceptions.PasswordRequired as e:
        raise PasswordRequired("ファイル名まで暗号化された 7z は一覧を出せません（未対応）") from e
    except (py7zr.exceptions.Bad7zFile, py7zr.exceptions.UnsupportedCompressionMethodError) as e:
        raise ArchiveError(f"7z として読めません: {e}") from e
    for info in infos:
        inner = safe_relpath(info.filename)
        if inner is None:
            index.skipped += 1
            continue
        stamp = info.creationtime.timestamp() if info.creationtime else 0.0
        index.add(inner, Member(info.filename, bool(info.is_directory),
                                int(info.uncompressed or 0), stamp, index.needs_password))
    return index


def _build_tar(path: str) -> ArchiveIndex:
    import tarfile  # noqa: PLC0415  標準だが書庫を開くときにだけ読む(zipfile と揃える)
    index = ArchiveIndex(path, "tar")
    try:
        tf = tarfile.open(path, "r:*")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ArchiveError(f"tar として読めません: {e}") from e
    with tf:
        try:
            members = tf.getmembers()
        except (tarfile.TarError, OSError, EOFError) as e:
            raise ArchiveError(f"tar として読めません: {e}") from e
        for info in members:
            inner = safe_relpath(info.name)
            if inner is None:
                index.skipped += 1
                continue
            index.add(inner, Member(info.name, bool(info.isdir()),
                                    int(info.size or 0), float(info.mtime or 0.0)))
    return index


# 開いた書庫の一覧は覚えておく(書庫の中で移動するたびに読み直すと、大きな書庫で遅い)。
# 書庫ファイルの更新日時と大きさが変われば読み直す。読み取りスレッドから触るので鍵を掛ける。
_cache = {}
_cache_lock = threading.Lock()
_CACHE_LIMIT = 8


def get_index(path: str, password: str = None) -> ArchiveIndex:
    """書庫の索引。password はファイル名まで暗号化された 7z の一覧用
    (パスワード付きで作った索引は覚えない。パスワード自体も残さない)。"""
    st = os.stat(path)
    key = os.path.normcase(os.path.abspath(path))
    stamp = (st.st_mtime, st.st_size)
    if password is None:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1]
    kind = archive_kind(path)
    if kind == "7z":
        index = _build_7z(path, password)
    elif kind == "tar":
        index = _build_tar(path)
    else:
        index = _build_zip(path)
    if password is None:
        with _cache_lock:
            _cache[key] = (stamp, index)
            while len(_cache) > _CACHE_LIMIT:
                _cache.pop(next(iter(_cache)))
    return index


def read_archive_folder(archive: str, inner: str, walk_up: bool = False):
    """書庫の中のフォルダを読む。(表示パス, 項目, (書庫, 中のパス), 除いた数)。"""
    index = get_index(archive)
    inner = safe_relpath(inner) or ""
    while not index.has_dir(inner):
        if not walk_up:
            raise FileNotFoundError(f"書庫の中にありません: {display_path(archive, inner)}")
        inner = inner.rpartition("/")[0]
    return display_path(archive, inner), index.list_dir(inner), (archive, inner), index.skipped


# ---------------------------------------------------------------
# 展開
# ---------------------------------------------------------------
@dataclass
class PlanItem:
    inner: str      # 書庫の中のパス
    rel: str        # 展開先からの相対パス(/ 区切り)
    is_dir: bool


def plan_extract(index: ArchiveIndex, base: str, names) -> list:
    """base(いま見ている中のフォルダ)にある names を、中身ごと展開する計画。

    展開先には base からの相対パスで置く(base の外のものは置かない)。危ないパスは
    ここでも除く(build_index で除いてあるが、二重に守る)。"""
    plan = []
    seen = set()
    for name in names:
        top = join_inner(base, name)
        member = index.members.get(top)
        if member is None:
            continue
        for inner in [top] + (index.descendants(top) if member.is_dir else []):
            rel = safe_relpath(inner[len(base) + 1:] if base else inner)
            if rel is None or inner in seen:
                continue
            seen.add(inner)
            plan.append(PlanItem(inner, rel, index.members[inner].is_dir))
    return plan


def whole_plan(index: ArchiveIndex, archive_name: str):
    """書庫全体を展開する計画と、単位になるフォルダ名。(計画, 単位フォルダ名)。

    U の展開用。トップが単一フォルダだけならそのまま展開先へ出す(単位はその
    フォルダ)。そうでなければ書庫名のフォルダを作ってその中へ出す(単位は書庫名の
    フォルダ)。計画の rel は展開先からの相対で、単位フォルダを含む。"""
    tops = [e.name for e in index.list_dir("")]
    plan = plan_extract(index, "", tops)
    if len(tops) == 1:
        member = index.members.get(tops[0])
        if member is not None and member.is_dir:
            return plan, tops[0]
    stem = archive_stem(archive_name)
    for item in plan:
        item.rel = f"{stem}/{item.rel}"
    return plan, stem


def find_conflicts(plan, dest: str) -> list:
    """展開先に既にあるファイル(上書きになるもの)の相対パス。フォルダは中へ重ねるので数えない。"""
    found = []
    for item in plan:
        if item.is_dir:
            continue
        if os.path.lexists(os.path.join(dest, item.rel.replace("/", os.sep))):
            found.append(item.rel)
    return found


# 「中身が同じ」とみなす更新日時のずれ(秒)。ZIP の日時は2秒刻みなので、それ以下は同じ。
SAME_MTIME_SLACK = 2.0


@dataclass
class Conflict:
    """展開先に既にあるファイル1件の、書庫の中のものとの比べ(上書き確認に出す)。"""
    rel: str
    archive_size: int
    archive_mtime: float          # 0 は不明
    existing_size: int
    existing_mtime: float

    @property
    def same(self) -> bool:
        """大きさと更新日時が同じ(＝前に同じ書庫から展開したもの)。尋ねずに飛ばしてよい。
        日時が分からないものは同じと言い切れないので False。"""
        return (bool(self.archive_mtime) and self.archive_size == self.existing_size
                and abs(self.archive_mtime - self.existing_mtime) <= SAME_MTIME_SLACK)

    @property
    def existing_newer(self) -> bool:
        """今あるもののほうが新しい(上書きすると新しいものが古いもので消える)。"""
        return (bool(self.archive_mtime)
                and self.existing_mtime > self.archive_mtime + SAME_MTIME_SLACK)

    def describe(self) -> str:
        """上書き確認に出す2行(書庫の中／今あるもの。新しいほうに印)。"""
        archive_time = core.format_mtime(self.archive_mtime) or "日時不明"
        existing_time = core.format_mtime(self.existing_mtime) or "日時不明"
        newer_mark = "  ← こちらが新しい"
        archive_newer = bool(self.archive_mtime) and \
            self.archive_mtime > self.existing_mtime + SAME_MTIME_SLACK
        return (f"書庫の中:　{archive_time}　{self.archive_size:,} バイト"
                f"{newer_mark if archive_newer else ''}\n"
                f"今あるもの: {existing_time}　{self.existing_size:,} バイト"
                f"{newer_mark if self.existing_newer else ''}")


def describe_conflicts(index: ArchiveIndex, plan, dest: str) -> list:
    """find_conflicts の各件を、書庫の中と今あるものの大きさ・日時で比べた Conflict の並び。
    今あるものが読めなければ(フォルダが同名で居るなど)日時0・大きさ-1 として必ず尋ねる側に。"""
    out = []
    by_rel = {item.rel: item for item in plan if not item.is_dir}
    for rel in find_conflicts(plan, dest):
        member = index.members.get(by_rel[rel].inner) if rel in by_rel else None
        target = os.path.join(dest, rel.replace("/", os.sep))
        try:
            st = os.stat(target)
            existing_size = st.st_size if os.path.isfile(target) else -1
            existing_mtime = st.st_mtime
        except OSError:
            existing_size, existing_mtime = -1, 0.0
        out.append(Conflict(rel,
                            member.size if member is not None else -1,
                            member.mtime if member is not None else 0.0,
                            existing_size, existing_mtime))
    return out


def needs_password(index: ArchiveIndex, plan) -> bool:
    if index.kind == "7z":
        return index.needs_password and any(not p.is_dir for p in plan)
    return any(index.members[p.inner].encrypted for p in plan if not p.is_dir)


def _target_path(dest: str, rel: str) -> str:
    """展開先の実パス。dest の外を指していたら None(最後の砦)。"""
    target = os.path.abspath(os.path.join(dest, rel.replace("/", os.sep)))
    root = os.path.abspath(dest)
    if os.path.normcase(target) != os.path.normcase(root) and not os.path.normcase(
            target).startswith(os.path.normcase(root.rstrip(os.sep) + os.sep)):
        return None
    return target


@dataclass
class ExtractResult:
    written: int = 0
    skipped: int = 0
    errors: list = None
    cancelled: bool = False

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def extract(index: ArchiveIndex, plan, dest: str, skip=(), password: str = None,
            progress=None, cancel: threading.Event = None) -> ExtractResult:
    """plan を dest へ展開する。読み取りスレッドで呼ぶ(Qt を触らない)。

    skip は上書きしないと決めた相対パスの集まり。progress(済んだ数, 全体, 名前) を
    1件ごとに呼ぶ。cancel が立ったら次のファイルの前(と大きなファイルの途中)で止める。
    途中で止めたファイルは消す(半端なファイルを残さない)。"""
    result = ExtractResult()
    skip = set(skip)
    files = [p for p in plan if not p.is_dir]
    total = len(files)
    cancel = cancel or threading.Event()
    for item in plan:
        if item.is_dir:
            target = _target_path(dest, item.rel)
            if target is None:
                result.errors.append(f"{item.rel}: 展開先の外を指しています")
                continue
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as e:
                result.errors.append(f"{item.rel}: {e}")
    if index.kind == "7z":
        _extract_7z(index, files, dest, skip, password, progress, cancel, result, total)
    elif index.kind == "tar":
        _extract_tar(index, files, dest, skip, password, progress, cancel, result, total)
    else:
        _extract_zip(index, files, dest, skip, password, progress, cancel, result, total)
    return result


def _extract_zip(index, files, dest, skip, password, progress, cancel, result, total):
    if any(index.members[p.inner].compress_type == 99 for p in files):
        # WinZip AES 形式は zipfile では開けない。pyzipper に任せる。
        _extract_zip_aes(index, files, dest, skip, password, progress, cancel,
                         result, total)
        return
    pwd = password.encode("utf-8") if password else None
    try:
        zf = zipfile.ZipFile(index.path)
    except (OSError, zipfile.BadZipFile) as e:
        result.errors.append(str(e))
        return
    with zf:
        for done, item in enumerate(files):
            if cancel.is_set():
                result.cancelled = True
                return
            if progress:
                progress(done, total, item.rel)
            if item.rel in skip:
                result.skipped += 1
                continue
            target = _target_path(dest, item.rel)
            if target is None:
                result.errors.append(f"{item.rel}: 展開先の外を指しています")
                continue
            member = index.members[item.inner]
            # 隣の一時ファイルへ書いてから置き換える。直接 target へ書くと、上書きを選んだ
            # ファイルがパスワード違いや壊れた書庫で失敗したとき、元のファイルを消してしまう。
            part = target + PART_SUFFIX
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member.source, pwd=pwd) as src, open(part, "wb") as out:
                    while True:
                        if cancel.is_set():
                            break
                        chunk = src.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                if cancel.is_set():
                    _remove_quietly(part)
                    result.cancelled = True
                    return
                if member.mtime:
                    os.utime(part, (member.mtime, member.mtime))
                os.replace(part, target)
                result.written += 1
            except RuntimeError as e:
                # zipfile はパスワード違い・無しを RuntimeError で知らせる。
                _remove_quietly(part)
                result.errors.append(f"{item.rel}: パスワードが違うか、ありません（{e}）")
            except NotImplementedError as e:
                _remove_quietly(part)
                result.errors.append(f"{item.rel}: 未対応の圧縮・暗号です（{e}）")
            except (OSError, zipfile.BadZipFile, EOFError) as e:
                _remove_quietly(part)
                result.errors.append(f"{item.rel}: {e}")
    if progress:
        progress(total, total, "")


def _import_pyzipper():
    try:
        import pyzipper  # noqa: PLC0415  AES 形式の ZIP を開くときにだけ読む
    except ImportError as e:
        raise ArchiveError("AES 形式の ZIP を開くには pyzipper が必要です"
                           "（pip install pyzipper）") from e
    return pyzipper


def _extract_zip_aes(index, files, dest, skip, password, progress, cancel, result, total):
    """AES 形式の ZIP の展開。_extract_zip と同じ進み方(一時ファイル→置き換え、
    中止で半端を残さない)で、開く所だけ pyzipper。"""
    try:
        pyzipper = _import_pyzipper()
    except ArchiveError as e:
        result.errors.append(str(e))
        return
    try:
        zf = pyzipper.AESZipFile(index.path)
    except (OSError, zipfile.BadZipFile) as e:
        result.errors.append(str(e))
        return
    with zf:
        if password:
            zf.setpassword(password.encode("utf-8"))
        for done, item in enumerate(files):
            if cancel.is_set():
                result.cancelled = True
                return
            if progress:
                progress(done, total, item.rel)
            if item.rel in skip:
                result.skipped += 1
                continue
            target = _target_path(dest, item.rel)
            if target is None:
                result.errors.append(f"{item.rel}: 展開先の外を指しています")
                continue
            member = index.members[item.inner]
            part = target + PART_SUFFIX
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member.source) as src, open(part, "wb") as out:
                    while True:
                        if cancel.is_set():
                            break
                        chunk = src.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                if cancel.is_set():
                    _remove_quietly(part)
                    result.cancelled = True
                    return
                if member.mtime:
                    os.utime(part, (member.mtime, member.mtime))
                os.replace(part, target)
                result.written += 1
            except Exception as e:  # noqa: BLE001  パスワード違いの例外の型が一定しない
                _remove_quietly(part)
                text = str(e).lower()
                if "password" in text or "decrypt" in text or "aes" in text:
                    result.errors.append(f"{item.rel}: パスワードが違うか、ありません（{e}）")
                else:
                    result.errors.append(f"{item.rel}: {e}")
    if progress:
        progress(total, total, "")


def _extract_tar(index, files, dest, skip, password, progress, cancel, result, total):
    """tar 系の展開。_extract_zip と同じ進み方(一時ファイル→置き換え)。パスワードは無い。"""
    import tarfile  # noqa: PLC0415
    try:
        tf = tarfile.open(index.path, "r:*")
    except (tarfile.TarError, OSError, EOFError) as e:
        result.errors.append(str(e))
        return
    with tf:
        for done, item in enumerate(files):
            if cancel.is_set():
                result.cancelled = True
                return
            if progress:
                progress(done, total, item.rel)
            if item.rel in skip:
                result.skipped += 1
                continue
            target = _target_path(dest, item.rel)
            if target is None:
                result.errors.append(f"{item.rel}: 展開先の外を指しています")
                continue
            member = index.members[item.inner]
            part = target + PART_SUFFIX
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member.source)
                if src is None:
                    raise OSError("中身を取り出せません")
                with src, open(part, "wb") as out:
                    while True:
                        if cancel.is_set():
                            break
                        chunk = src.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                if cancel.is_set():
                    _remove_quietly(part)
                    result.cancelled = True
                    return
                if member.mtime:
                    os.utime(part, (member.mtime, member.mtime))
                os.replace(part, target)
                result.written += 1
            except (OSError, tarfile.TarError, EOFError) as e:
                _remove_quietly(part)
                result.errors.append(f"{item.rel}: {e}")
    if progress:
        progress(total, total, "")


def _extract_7z(index, files, dest, skip, password, progress, cancel, result, total):
    """7z は1件ずつ取り出す口が無い(固体圧縮)ので、いったん作業用フォルダへまとめて
    展開してから、1件ずつ展開先へ移す。移す段で上書きの可否と中止を見る。"""
    wanted = [p for p in files if p.rel not in skip]
    result.skipped += len(files) - len(wanted)
    if not wanted:
        return
    try:
        py7zr = _import_py7zr()
    except ArchiveError as e:
        result.errors.append(str(e))
        return
    os.makedirs(TEMP_ROOT, exist_ok=True)
    staging = tempfile.mkdtemp(prefix="x7z_", dir=TEMP_ROOT)
    try:
        if progress:
            progress(0, total, "（7z を展開しています）")
        try:
            with py7zr.SevenZipFile(index.path, "r", password=password or None) as z:
                z.extract(path=staging, targets=[index.members[p.inner].source for p in wanted])
        except Exception as e:  # noqa: BLE001  py7zr は壊れ方・パスワード違いで色々投げる
            result.errors.append(f"7z を展開できません（パスワード違いの可能性も）: {e}")
            return
        for done, item in enumerate(wanted):
            if cancel.is_set():
                result.cancelled = True
                return
            if progress:
                progress(done, total, item.rel)
            source = _target_path(staging, index.members[item.inner].source)
            target = _target_path(dest, item.rel)
            if source is None or target is None:
                result.errors.append(f"{item.rel}: 展開先の外を指しています")
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.isfile(target):
                    os.remove(target)
                shutil.move(source, target)
                result.written += 1
            except OSError as e:
                result.errors.append(f"{item.rel}: {e}")
        if progress:
            progress(total, total, "")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _remove_quietly(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------
# 一時フォルダ(書庫の中のファイルを開くとき)
# ---------------------------------------------------------------
def make_temp_dir() -> str:
    os.makedirs(TEMP_ROOT, exist_ok=True)
    return tempfile.mkdtemp(prefix="open_", dir=TEMP_ROOT)


def cleanup_temp(older_than: float = 0.0) -> int:
    """一時フォルダの中を片付ける。older_than 秒より古いものだけ(0 なら全部)。
    開いたままのファイルがあれば消せずに残る(次回に持ち越す)。消した数を返す。"""
    removed = 0
    try:
        names = os.listdir(TEMP_ROOT)
    except OSError:
        return 0
    cutoff = time.time() - older_than
    for name in names:
        path = os.path.join(TEMP_ROOT, name)
        try:
            if older_than and os.path.getmtime(path) > cutoff:
                continue
            shutil.rmtree(path)
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------
# ZIP を作る(ファイラの P)
# ---------------------------------------------------------------
@dataclass
class ZipResult:
    files: int = 0
    folders: int = 0
    source_bytes: int = 0
    zip_bytes: int = 0
    skipped: list = None      # 読めずに入れられなかったもの("名前: 理由")
    cancelled: bool = False
    error: str = ""           # これがあれば ZIP は作っていない

    def __post_init__(self):
        if self.skipped is None:
            self.skipped = []


def create_zip(folder: str, names, dest: str, progress=None,
               cancel: threading.Event = None) -> ZipResult:
    """folder の names(フォルダは中身ごと)を dest に ZIP で作る。読み取りスレッドで呼ぶ。

    - ZIP_DEFLATED。名前に ASCII 以外があれば zipfile が UTF-8 の印を付けて書く
      (Windows 標準の展開でも化けない)。更新日時は元のファイルのまま
    - 隣の .filer-part に書いてから置き換える。中止・失敗のときは作りかけを消す
      (途中までの ZIP が本物の名前で残ると、壊れた書庫を正しいものと取り違える)
    - 作ろうとしている ZIP 自身は入れない(いまのフォルダに作るときの自己参照)
    - 読めないファイル(使用中など)は飛ばして続け、skipped に残す"""
    result = ZipResult()
    cancel = cancel or threading.Event()
    part = dest + ".filer-part"
    try:
        plan = core.zip_plan(folder, names, exclude=dest)
        total = len(plan)
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
            for done, (path, arc, is_dir) in enumerate(plan):
                if cancel.is_set():
                    result.cancelled = True
                    break
                if progress:
                    progress(done, total, arc)
                try:
                    zf.write(path, arc)
                except OSError as e:
                    result.skipped.append(f"{arc}: {e}")
                    continue
                if is_dir:
                    result.folders += 1
                else:
                    result.files += 1
                    try:
                        result.source_bytes += os.path.getsize(path)
                    except OSError:
                        pass
        if result.cancelled:
            _remove_quietly(part)
            return result
        os.replace(part, dest)
        result.zip_bytes = os.path.getsize(dest)
        if progress:
            progress(total, total, "")
    except Exception as e:  # noqa: BLE001  ディスクが一杯など。作りかけは残さない
        _remove_quietly(part)
        result.error = f"{type(e).__name__}: {e}"
    return result


def create_7z(folder: str, names, dest: str, progress=None,
              cancel: threading.Event = None) -> ZipResult:
    """folder の names(フォルダは中身ごと)を dest に 7z で作る。create_zip の 7z 版。

    - py7zr の既定(LZMA2)で圧縮する。読み取りスレッドで呼ぶ
    - ZIP と同じく隣の .filer-part に書いてから置き換える(中止・失敗のときに
      作りかけは残さない)。作ろうとしている書庫自身は入れない
    - 計画(core.zip_plan)の順に1件ずつ書く。フォルダは空のときだけ実エントリを
      持ち、中身のあるフォルダは中のファイルのパスから辿れるので数だけ数える
      (7z ではフォルダの実エントリは必須ではない。あっふの読み取りも合成で補う)
    - 読めないファイル(使用中など)は飛ばして続け、skipped に残す"""
    result = ZipResult()
    cancel = cancel or threading.Event()
    part = dest + ".filer-part"
    try:
        py7zr = _import_py7zr()
        plan = core.zip_plan(folder, names, exclude=dest)
        total = len(plan)
        with py7zr.SevenZipFile(part, "w") as zf:
            for done, (path, arc, is_dir) in enumerate(plan):
                if cancel.is_set():
                    result.cancelled = True
                    break
                if progress:
                    progress(done, total, arc)
                if is_dir:
                    result.folders += 1
                    try:
                        if not os.listdir(path):
                            zf.writeall(path, arc.rstrip("/"))
                    except OSError as e:
                        result.skipped.append(f"{arc}: {e}")
                    continue
                try:
                    zf.writeall(path, arc)
                except OSError as e:
                    result.skipped.append(f"{arc}: {e}")
                    continue
                result.files += 1
                try:
                    result.source_bytes += os.path.getsize(path)
                except OSError:
                    pass
        if result.cancelled:
            _remove_quietly(part)
            return result
        os.replace(part, dest)
        result.zip_bytes = os.path.getsize(dest)
        if progress:
            progress(total, total, "")
    except Exception as e:  # noqa: BLE001  py7zr が無い・ディスクが一杯など。作りかけは残さない
        _remove_quietly(part)
        result.error = f"{type(e).__name__}: {e}"
    return result
