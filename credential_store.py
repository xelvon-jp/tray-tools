# credential_store.py
# Windows の資格情報マネージャ(Credential Manager)へ秘密を預ける小さな窓口。
# トレイアイコンは持たないただの部品(keep_awake.py / window_tools.py と同じ立ち位置)。
#
# なぜこれが要るか:
#   settings.json は手で編集する前提の平文ファイルで、そこへ API トークンを書くと
#   バックアップにも差分にもそのまま乗る。秘密は OS の保管庫に預け、設定ファイルには
#   「登録されているかどうか」すら書かない。
#
# なぜ keyring ではないか:
#   依存を1つも増やさずに済むため。必要なのは advapi32 の CredWriteW / CredReadW /
#   CredDeleteW の3つだけで、ctypes で直に叩ける。ブロブの文字コードは keyring の
#   Windows バックエンドに合わせて UTF-16LE にしてあるので、あとで keyring を入れても
#   同じ資格情報を読める。
#
# ここの関数は例外を投げない。呼ぶ側(トレイメニュー・IPCハンドラ)は Qt のスロットの中で
# あり、PySide6 はスロットから例外が抜けると常駐アプリごと終了するため。
# 失敗は False / None で返す。
import ctypes
import sys
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
# LOCAL_MACHINE はこのPCにだけ残る(3=ENTERPRISE はドメインで漫遊しうる)。
# トークンを他のPCへ勝手に持ち出させない。
CRED_PERSIST_LOCAL_MACHINE = 2

# ブロブの上限(CRED_MAX_CREDENTIAL_BLOB_SIZE)。超えると CredWriteW が失敗する。
MAX_BLOB_BYTES = 5 * 512


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIAL(ctypes.Structure):
    """advapi32 の CREDENTIALW。フィールドの並びと型は変えないこと
    (構造体はそのままAPIへ渡すので、1つでもずれると別の場所を読み書きする)。"""

    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_CredWriteW = _advapi32.CredWriteW
_CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
_CredWriteW.restype = wintypes.BOOL

_CredReadW = _advapi32.CredReadW
_CredReadW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
]
_CredReadW.restype = wintypes.BOOL

_CredDeleteW = _advapi32.CredDeleteW
_CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
_CredDeleteW.restype = wintypes.BOOL

_CredFree = _advapi32.CredFree
_CredFree.argtypes = [ctypes.c_void_p]
_CredFree.restype = None


def _complain(where: str) -> None:
    """失敗の理由を標準エラーへ。トークンそのものは絶対に書かないこと。

    pythonw 起動では誰も読めないが、デバッグ起動(TrayTools-debug.bat)では見える。"""
    print(f"[tray-tools] 資格情報の{where}に失敗しました "
          f"(GetLastError={ctypes.get_last_error()})", file=sys.stderr)


def write(target: str, username: str, secret: str) -> bool:
    """資格情報マネージャへ1件書く。同じ target があれば置き換える。

    username は資格情報マネージャの一覧に平文で並ぶ。秘密にしたい値は secret へ。"""
    try:
        blob = (secret or "").encode("utf-16-le")
        if len(blob) > MAX_BLOB_BYTES:
            print("[tray-tools] 資格情報が長すぎます", file=sys.stderr)
            return False
        # ctypes の一時バッファは、APIを呼び終えるまで参照を残しておくこと
        # (式の途中で作ると解放済みの領域を渡しうる)。
        buffer = ctypes.create_string_buffer(blob, len(blob))
        cred = _CREDENTIAL()
        cred.Flags = 0
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = None
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = username or " "  # 空文字は受け付けられない
        if not _CredWriteW(ctypes.byref(cred), 0):
            _complain("保存")
            return False
        return True
    except Exception:
        _complain("保存")
        return False


def read(target: str):
    """(username, secret) を返す。無ければ None。

    CredReadW が返す領域は API 側の持ち物なので、必要な値をPythonの文字列へ写してから
    必ず CredFree で返す(ここを忘れると呼ぶたびに漏れる)。"""
    pointer = ctypes.POINTER(_CREDENTIAL)()
    try:
        if not _CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return None  # 未登録は珍しくないので、ここでは何も言わない
    except Exception:
        _complain("読み出し")
        return None
    try:
        cred = pointer.contents
        size = int(cred.CredentialBlobSize)
        raw = ctypes.string_at(cred.CredentialBlob, size) if size else b""
        # 書いた側と同じ UTF-16LE で読む。他所のツールが UTF-8 で書いた資格情報を
        # 掴んだときのために、奇数長・復号失敗は UTF-8 で読み直す。
        try:
            secret = raw.decode("utf-16-le") if size % 2 == 0 else raw.decode("utf-8")
        except UnicodeDecodeError:
            secret = raw.decode("utf-8", errors="replace")
        username = cred.UserName or ""
        return username.strip(), secret
    except Exception:
        _complain("読み出し")
        return None
    finally:
        _CredFree(ctypes.cast(pointer, ctypes.c_void_p))


def delete(target: str) -> bool:
    """1件消す。もともと無ければ False(消せなかったのではなく、消すものが無い)。"""
    try:
        return bool(_CredDeleteW(target, CRED_TYPE_GENERIC, 0))
    except Exception:
        _complain("削除")
        return False


def exists(target: str) -> bool:
    """登録されているか。中身は読まない側の呼び出し向け(メニューの表示など)。"""
    return read(target) is not None
