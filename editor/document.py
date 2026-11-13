# editor/document.py
# テキストエディタの文書モデル(Qt に依存しない部分)。
#
# 守ること(仕様): 保存のとき、開いたときの文字コードと改行(CRLF/LF)を
# そのまま守る。変換は利用者が選んだときだけ。だから読み込みでは
# 「文字コード・BOM の有無・改行」を控えておき、保存ではそれを使って
# バイト列に戻す。本文は編集しやすいよう、改行を "\n" にそろえて持つ。
#
# 文字コードの判定は text_encoding(ビュアと共用)に任せる。
#
# 保存の安全策(第3段):
#   - 書き込みは原子的(同じフォルダの一時ファイルに書いてから置き換える)。
#     途中で落ちても元のファイルは壊れない
#   - 上書きの前に、それまでの内容の控えを「元の名前 + .bak」に1つだけ
#     残す(増え続けない形)。フォルダ内の横断置換だけは、最初の控えが
#     後からの置換で潰れないよう、連番の別名に逃がす形も選べる
#     (save_document の numbered_backup)
#   - 開いたときと保存したときの、ディスク上の (更新時刻, 大きさ) を
#     控えておく。外部で変わったかの判定(窓の監視と、保存直前の確認)
#     はこれと比べる
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass

import text_encoding

CRLF = "\r\n"
LF = "\n"
CR = "\r"
NEWLINE_LABELS = {CRLF: "CRLF", LF: "LF"}

# 判定に使う先頭の長さ(filer_viewer.DETECT_BYTES と同じ)。
DETECT_HEAD_BYTES = 4 * 1024 * 1024

# 新規文書の既定。相手は Windows の道具なので、文字コードは BOM 無し
# UTF-8、改行は CRLF。
DEFAULT_ENCODING = "utf-8"
DEFAULT_NEWLINE = CRLF


@dataclass
class TextDocument:
    path: str = None            # 保存先。無題なら None
    encoding: str = DEFAULT_ENCODING
    bom: bool = False           # 開いたときに BOM が付いていたか
    newline: str = DEFAULT_NEWLINE
    text: str = ""              # 本文。改行は "\n" に正規化済み
    line_newlines: list = None  # 行ごとの改行種別(CRLF/LF/CR、最終行は None)。
                                # 読み込んだ元の並びの控え(改行記号の描き
                                # 分けと、「混在のまま」保存の材料)
    lossy_decode: bool = False  # 読み替え(置換文字)が発生したか
    mixed_newlines: bool = False  # CRLF と LF が混在していたか
    disk_mtime: float = None    # ディスク上の更新時刻(開いた/保存した時点)
    disk_size: int = None       # ディスク上の大きさ(バイト数)

    @property
    def display_name(self) -> str:
        if not self.path:
            return "無題"
        import os

        return os.path.basename(self.path)


def detect_newline(text: str) -> str:
    """本文で最初に出てくる改行の種類。改行が無ければ CRLF(Windows の道具
    としての既定)。混在していても、文書の「改行設定」としては最初の
    ものに合わせる(ステータス表示と、新しく増えた行の改行の既定。
    保存で混在をどう扱うかは、利用者が保存のときに選ぶ)。"""
    index = text.find("\n")
    if index > 0 and text[index - 1] == "\r":
        return CRLF
    if index == -1:
        return DEFAULT_NEWLINE
    return LF


def has_mixed_newlines(text: str) -> bool:
    """CRLF と LF(CR を伴わない改行)が両方あるか。混在は、開いたときに
    利用者へ一度知らせるための判定。保存でどう扱うか(混在のままか、
    どちらかへ統一か)は、明示的な保存のときに窓が利用者へ聞く。"""
    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    return crlf > 0 and lone_lf > 0


def kinds_are_mixed(kinds) -> bool:
    """行ごとの改行種別の並び(line_endings の形)に、CRLF と LF が
    両方あるか。編集中の文書の「いま混在しているか」は、読み込み
    時点の控えではなく、本文が追随させている並びで判定する。"""
    if not kinds:
        return False
    return CRLF in kinds and LF in kinds


def join_with_line_newlines(text: str, kinds, default: str) -> str:
    """本文(改行は "\\n" にそろえてある)を行ごとの改行種別で
    つなぎ直す(「混在のまま」保存するための組み立て)。

    kinds は line_endings の形(行数と同じ長さで、最終行は None)。
    行数に足りない・余る場合は、足りないぶんを default(文書の改行
    設定)で補い、余りは無視する。種別としてあり得ない値が入って
    いた行も default に倒す(編集でずれた控えでも壊れない倒し方で、
    text_edit の描画側の「控えが無い行は文書の改行で描く」と同じ)。"""
    lines = text.split("\n")
    if len(lines) == 1:
        return text
    out = [lines[0]]
    for index in range(1, len(lines)):
        prev = index - 1
        kind = kinds[prev] if kinds is not None and prev < len(kinds) else None
        out.append(kind if kind in (CRLF, LF, CR) else default)
        out.append(lines[index])
    return "".join(out)


def replaced_line_newlines(old_text, old_kinds, replacements, default):
    """置換後の本文の、行ごとの改行種別の並び(line_endings の形)を作る。

    replacements は (開始, 終了, 置き換えた文字列) の並びで、位置は
    置換前の本文(old_text。改行は "\\n" にそろえてある)でのもの、
    重ならず昇順に並んでいる前提。置換されなかった部分の改行は
    元の種別をそのまま引き継ぐので、「置換していない行の改行は
    変わらない」。置き換えた文字列の中に新しく生まれた改行は、
    置換が元の改行を取り込んでいたときは取り込んだ種別を順に、
    そうでなければ default(文書の改行設定)にする。

    フォルダ内の横断置換(greplace)が、混在したファイルでも
    対話の保存と同じ扱い(行ごとの改行を保つ)にするために使う。"""
    kinds = []
    cursor = 0   # 置換前の本文での走査位置
    line = 0     # cursor がいる置換前の行の番号(old_kinds の添字)

    def kind_at(index):
        if old_kinds is not None and 0 <= index < len(old_kinds):
            kind = old_kinds[index]
            if kind in (CRLF, LF, CR):
                return kind
        return default

    def copy_until(pos):
        """cursor から pos までの、置換されなかった部分の改行を移す。"""
        nonlocal cursor, line
        for _ in range(old_text.count("\n", cursor, pos)):
            kinds.append(kind_at(line))
            line += 1
        cursor = pos

    for start, end, inserted in replacements:
        copy_until(start)
        consumed = [kind_at(line + offset)
                    for offset in range(old_text.count("\n", start, end))]
        for index in range(inserted.count("\n")):
            if index < len(consumed):
                kinds.append(consumed[index])
            elif consumed:
                kinds.append(consumed[-1])
            else:
                kinds.append(default)
        line += len(consumed)
        cursor = end
    copy_until(len(old_text))
    kinds.append(None)
    return kinds


# 改行の並びを切り出すための正規表現。CRLF を先に置くことで、"\r\n" が
# CR と LF の2つに割れない(1文字ずつ走査する形と結果が一致する)。
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


def line_endings(text: str) -> list:
    """行ごとの改行種別の並びを返す(正規化する前の本文から取る)。

    値は CRLF / LF / CR のいずれかで、その行を終える改行が無い行
    (最後の行)は None。並びの長さは、改行を "\\n" にそろえた本文の
    行数(改行の数 + 1)と一致する。本文ウィジェット(Qt も CR・LF・
    CRLF のどれでも段落を分ける)のブロックと、番号がそのまま対応
    する。不可視文字の表示で、行ごとに改行の種類を描き分けるための
    控えで、「混在のまま」保存するときの材料でもある。

    大きなファイルでも目立って遅くならないよう、改行の切り出しは
    正規表現(C 側の走査)に任せる。"""
    kinds = _NEWLINE_RE.findall(text)
    kinds.append(None)
    return kinds


def _disk_stamp(path):
    """(更新時刻, 大きさ)。読めなければ (None, None)。"""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    return st.st_mtime, st.st_size


def external_changed(doc: TextDocument) -> bool:
    """ディスク上のファイルが、開いた/保存した時点から変わったか。

    控え(更新時刻と大きさ)が無い(無題・新規)なら「変わっていない」。
    ファイルが消えている場合は「変わった」側に倒す。"""
    if not doc.path or doc.disk_mtime is None:
        return False
    mtime, size = _disk_stamp(doc.path)
    if mtime is None:
        return True
    return mtime != doc.disk_mtime or size != doc.disk_size


def find_unencodable(text: str, encoding: str) -> list:
    """text の中で encoding で書けない文字を、現れた順に重複なく返す。

    保存の前に利用者へ確認するための材料。1文字ずつ試すので、対象は
    「相異なる文字」の数だけ(本文の長さには比例しない)。"""
    bad = []
    seen = set()
    for ch in text:
        if ch in seen:
            continue
        seen.add(ch)
        try:
            ch.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            bad.append(ch)
    return bad


def load_document(path: str) -> TextDocument:
    """ファイルを読み、文字コードと改行を控えた TextDocument を返す。

    読めないバイトがあったら置換文字で読み、lossy_decode を立てる
    (ビュアと同じ倒し方。保存すると置換文字がそのまま書かれるので、
    窓側はその旨を表示する)。"""
    with open(path, "rb") as f:
        data = f.read()
    encoding, start = text_encoding.detect_encoding(data[:DETECT_HEAD_BYTES])
    raw = data[start:]
    lossy = False
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        text = raw.decode(encoding, errors="replace")
        lossy = True
    newline = detect_newline(text)
    mixed = has_mixed_newlines(text)
    mtime, size = _disk_stamp(path)
    return TextDocument(
        path=path,
        encoding=encoding,
        bom=start > 0,
        newline=newline,
        text=text.replace("\r\n", "\n"),
        line_newlines=line_endings(text),
        lossy_decode=lossy,
        mixed_newlines=mixed,
        disk_mtime=mtime,
        disk_size=size,
    )


def encode_document(doc: TextDocument, encoding=None, newline=None, bom=None,
                    errors="strict", line_newlines=None) -> bytes:
    """doc をバイト列に戻す。encoding / newline / bom を渡すと、その
    変換で書く(「名前を付けて保存」で利用者が選んだ場合だけ渡す)。

    line_newlines(行ごとの改行種別の並び)を渡すと、改行はそろえず
    行ごとにその種別で書く(「混在のまま」の保存)。このとき newline
    は使わない(つなぎ直しの補いに使う既定は doc.newline)。
    渡さなければ、従来どおり doc.newline(または newline)で全体を
    そろえる。

    変換先の文字コードで書けない文字があると、既定(errors="strict")
    では UnicodeEncodeError を上げる。黙って置換文字にすると中身が
    壊れるので、呼び側が利用者に伝える前提で隠さない。errors="replace"
    は、利用者が確認した上で化けを承知で書くときだけ渡す。"""
    enc = encoding if encoding is not None else doc.encoding
    nl = newline if newline is not None else doc.newline
    use_bom = doc.bom if bom is None else bom
    text = doc.text
    if line_newlines is not None:
        text = join_with_line_newlines(text, line_newlines, doc.newline)
    elif nl == CRLF:
        text = text.replace("\n", "\r\n")
    body = text.encode(enc, errors=errors)
    return (text_encoding.bom_bytes(enc) if use_bom else b"") + body


# ---------------------------------------------------------------
# ファイル属性の引き継ぎ(原子的な保存は新しいファイルになるため)
# ---------------------------------------------------------------
# os.replace は一時ファイルと差し替えるので、置き換えた後のファイルは
# 「新しく作られたファイル」の属性になる: 隠し属性が外れ、作成日時が
# 保存の時刻になり、アクセス権も既定に戻る(レビュー req-20261113-107
# の指摘)。置き換えの前に元の属性を控え、後に引き継ぐ。
# NTFS の ACL(セキュリティ記述子そのもの)はここでは扱わない
# (引き継げるのは、アクセス権のビット・隠し等の属性・作成日時まで)。
def _capture_file_attrs(path):
    """置き換え前のファイルの属性の控え。読めなければ None。

    形: {"mode": アクセス権のビット,
         "windows": (属性ビット, 作成日時を表す整数) | None}"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    saved = {"mode": stat.S_IMODE(st.st_mode), "windows": None}
    if sys.platform == "win32":
        saved["windows"] = _windows_file_attrs(path)
    return saved


def _restore_file_attrs(path, saved):
    """控えた属性を、置き換えた後のファイルへ引き継ぐ。

    保存自体は済んでいるので、失敗しても例外は上げない(属性の
    引き継ぎ損ないで保存を失敗扱いにしない)。"""
    if not saved:
        return
    if saved.get("windows"):
        try:
            _apply_windows_file_attrs(path, saved["windows"])
        except Exception:  # noqa: BLE001  引き継ぎは最善を尽くすだけ
            pass
    try:
        os.chmod(path, saved["mode"])
    except OSError:
        pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _FILE_ATTRIBUTE_READONLY = 0x00000001
    _FILE_ATTRIBUTE_HIDDEN = 0x00000002
    _FILE_ATTRIBUTE_SYSTEM = 0x00000004
    # 保存で引き継ぐ属性ビット(アーカイブ等は新しいファイルの
    # 既定どおりに任せる)。
    _CARRY_ATTRS = (_FILE_ATTRIBUTE_READONLY | _FILE_ATTRIBUTE_HIDDEN
                    | _FILE_ATTRIBUTE_SYSTEM)
    _INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    _OPEN_EXISTING = 3

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    class _WIN32_FILE_ATTRIBUTE_DATA(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wintypes.DWORD),
                    ("ftCreationTime", _FILETIME),
                    ("ftLastAccessTime", _FILETIME),
                    ("ftLastWriteTime", _FILETIME),
                    ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD)]

    def _kernel32():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetFileAttributesW.restype = wintypes.DWORD
        kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR,
                                                wintypes.DWORD]
        kernel32.SetFileAttributesW.restype = wintypes.BOOL
        kernel32.GetFileAttributesExW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int,
            ctypes.POINTER(_WIN32_FILE_ATTRIBUTE_DATA)]
        kernel32.GetFileAttributesExW.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.SetFileTime.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)]
        kernel32.SetFileTime.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def _windows_file_attrs(path):
        """(属性ビット, 作成日時) を返す。取れなければ None。

        作成日時は FILETIME(1601年からの 100 ナノ秒単位)を1つの
        整数にして持つ。"""
        kernel32 = _kernel32()
        attrs = kernel32.GetFileAttributesW(path)
        if attrs == _INVALID_FILE_ATTRIBUTES:
            return None
        data = _WIN32_FILE_ATTRIBUTE_DATA()
        if not kernel32.GetFileAttributesExW(path, 0, ctypes.byref(data)):
            return None
        created = data.ftCreationTime
        creation = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return attrs, creation

    def _apply_windows_file_attrs(path, saved):
        """属性ビット(隠し・システム・読み取り専用)と作成日時を
        引き継ぐ。"""
        attrs, creation = saved
        kernel32 = _kernel32()
        current = kernel32.GetFileAttributesW(path)
        if current != _INVALID_FILE_ATTRIBUTES:
            merged = (current & ~_CARRY_ATTRS) | (attrs & _CARRY_ATTRS)
            if merged != current:
                kernel32.SetFileAttributesW(path, merged)
        handle = kernel32.CreateFileW(
            path, _GENERIC_WRITE, _FILE_SHARE_ALL, None,
            _OPEN_EXISTING, 0, None)
        invalid = ctypes.c_void_p(-1).value
        if handle and handle != invalid:
            try:
                filetime = _FILETIME(creation & 0xFFFFFFFF,
                                     (creation >> 32) & 0xFFFFFFFF)
                kernel32.SetFileTime(handle, ctypes.byref(filetime),
                                     None, None)
            finally:
                kernel32.CloseHandle(handle)


def _atomic_write(target: str, data: bytes):
    """target と同じフォルダの一時ファイルに書いてから置き換える。

    書き込みの途中で落ちても、元のファイルは無傷のまま残る
    (置き換えは os.replace で一気に行う)。失敗したら一時ファイルを
    消して例外を上げる。置き換えで新しいファイルになるぶん、
    元のファイルの属性(アクセス権・隠し属性・作成日時)は引き継ぐ。"""
    folder = os.path.dirname(os.path.abspath(target))
    tmp = os.path.join(
        folder, f".{os.path.basename(target)}.tmp{os.getpid()}")
    saved = _capture_file_attrs(target) if os.path.exists(target) else None
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _restore_file_attrs(target, saved)


def _next_backup_path(target):
    """上書きしない控えの置き場。

    「保存先 + .bak」が空いていればそこ、既にあれば .bak2、.bak3…
    の最初の空き(既にある控えはどれも潰さない)。"""
    candidate = target + ".bak"
    if not os.path.exists(candidate):
        return candidate
    serial = 2
    while os.path.exists(f"{target}.bak{serial}"):
        serial += 1
    return f"{target}.bak{serial}"


def save_document(doc: TextDocument, path=None, encoding=None, newline=None,
                  bom=None, backup=True, errors="strict",
                  line_newlines=None, numbered_backup=False) -> str:
    """doc をファイルへ書く。成功したら doc 側の控え(保存先・文字コード・
    改行・BOM・ディスク上の印)を、実際に書いた内容に合わせて更新して、
    書いたパスを返す。

    line_newlines を渡すと、行ごとの改行を保って書く(「混在のまま」)。
    渡さなければ全体を doc.newline(または newline)でそろえる。

    backup が真で、保存先に既存のファイルがあれば、上書きの前に
    それまでの内容を「保存先 + .bak」へ控える(控えは常に1つ)。
    numbered_backup が真なら、控えを上書きせず、.bak が既にあれば
    .bak2、.bak3…と連番の別名にする(フォルダ内の横断置換用。
    2回目以降の置換でも、最初の控えが残る形)。
    書き込み自体は原子的に行う(_atomic_write)。"""
    target = path if path is not None else doc.path
    if not target:
        raise ValueError("保存先が決まっていません")
    data = encode_document(doc, encoding=encoding, newline=newline, bom=bom,
                           errors=errors, line_newlines=line_newlines)
    if backup and os.path.exists(target):
        try:
            backup_path = _next_backup_path(target) \
                if numbered_backup else target + ".bak"
            shutil.copyfile(target, backup_path)
        except OSError:
            pass  # 控えが作れなくても保存自体は止めない
    _atomic_write(target, data)
    doc.path = target
    if encoding is not None:
        doc.encoding = encoding
    if newline is not None:
        doc.newline = newline
    if bom is not None:
        doc.bom = bom
    # 行ごとの種別の控えと混在の控えは、実際に書いた内容に合わせる。
    # 書いたバイト列を読み戻して数えるのが確実(本文に単独の CR が
    # 残っていると、改行の置き換えだけではそろわないため)。統一して
    # 書けば混在は落ち、混在のまま書けば混在のまま残る。
    enc = encoding if encoding is not None else doc.encoding
    written = data.decode(enc, errors="replace")
    doc.line_newlines = line_endings(written)
    doc.mixed_newlines = has_mixed_newlines(written)
    doc.disk_mtime, doc.disk_size = _disk_stamp(target)
    return target


# ---------------------------------------------------------------
# 検索(窓から使う純粋関数。tests/test_editor_document.py で確かめる)
# ---------------------------------------------------------------
def find_next(text: str, word: str, start: int, case_sensitive=False) -> int:
    """start 以後に word が出る位置。無ければ -1。巻き戻しは窓側の仕事。

    大文字小文字を区別しない場合は casefold で比べる。casefold で長さが
    変わる文字(ß 等)では、返る位置が元の文字列とずれることがある
    (ビュアの find_in_line と同じ割り切り)。"""
    if not word:
        return -1
    start = max(0, min(start, len(text)))
    if case_sensitive:
        return text.find(word, start)
    return text.casefold().find(word.casefold(), start)


def find_prev(text: str, word: str, start: int, case_sensitive=False) -> int:
    """start より前に word が最後に出る位置。無ければ -1。"""
    if not word:
        return -1
    start = max(0, min(start, len(text)))
    if case_sensitive:
        return text.rfind(word, 0, start)
    return text.casefold().rfind(word.casefold(), 0, start)
