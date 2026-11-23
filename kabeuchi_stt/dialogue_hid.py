# -*- coding: utf-8 -*-
"""AI対話モード: ヘッドセットの多機能ボタンを HID から直接読む（読み取り専用）。Qt/COM 非依存、ctypes のみ。

RegisterHotKey（dialogue_headset）が押下を受け取れないことがあったための、もう1つの検出経路。
- Windows の HID デバイスを列挙（SetupAPI）し、ベンダ ID / 製品 ID が一致し、入力レポートに
  Consumer Page 0x0C の Play/Pause（0xCD）を持つインターフェース（`MI_00&Col01` 相当）を見つけて、
  共有読み取り（FILE_SHARE_READ|WRITE・overlapped）で開き、専用スレッドで ReadFile を回す。
- 押した瞬間のレポート `01 01 00…`（レポート ID 1）を押下として通知する。離し `01 00` は無視（パルスで同時刻）。
- **読み取り専用**。ヘッドセットへ何も書かない（WriteFile・HidD_SetFeature・SetOutputReport は使わない）。
- フックではない。デバイスを開いて読むだけ。停止はイベント＋CancelIoEx で確実に止め、ハンドルを閉じる。
- 副作用: 同じ押下は Windows にも届くので、再生中のメディアが止まる／動くことがある
  （RegisterHotKey で独占できていれば抑えられる。dialogue_headset が両方を併用する）。
- import しただけでは何も起きない（Win32 の DLL は初めて使うときに読む）。
- 純関数（レポートの解釈・パス名の絞り込み・候補の順位づけ・caps の判定）は Win32 なしでテストできる。
  読み取りの部品（lister / opener）は差し替え可。
"""
from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass
from typing import Callable

DEFAULT_VID_PID = "3511:2EF2"
USAGE_PAGE_CONSUMER = 0x0C
USAGE_PLAY_PAUSE = 0xCD
REPORT_ID_BUTTONS = 1
USAGE_PAGE_TELEPHONY = 0x0B      # マイクを使っている間（通話状態）は、ボタンの信号がこちら（電話用）に出る（実測 2026-11-23）
REPORT_ID_TELEPHONY = 2
DEFAULT_INPUT_LEN = 512
PRESS, RELEASE = "press", "release"


# ---------------------------------------------------------------------------
# 純関数

def parse_vid_pid(text: str) -> tuple[int, int] | None:
    """'3511:2EF2'（16進）→ (0x3511, 0x2EF2)。'VID_3511&PID_2EF2' も可。読めなければ None。"""
    s = (text or "").strip().lower().replace("vid_", "").replace("pid_", "").replace("&", ":")
    parts = [p.strip() for p in s.split(":") if p.strip()]
    if len(parts) != 2:
        return None
    try:
        v, p = int(parts[0], 16), int(parts[1], 16)
    except ValueError:
        return None
    if not (0 <= v <= 0xFFFF and 0 <= p <= 0xFFFF):
        return None
    return v, p


def path_matches(path: str, vid: int, pid: int) -> bool:
    """デバイスパスがその VID/PID か（`...#vid_3511&pid_2ef2&mi_00&col01#...`。大文字小文字は問わない）。"""
    low = (path or "").lower()
    return f"vid_{vid:04x}&pid_{pid:04x}" in low


def is_col01(path: str) -> bool:
    return "&col01" in (path or "").lower()


def short_path(path: str) -> str:
    """診断表示用にパスを短く（`mi_00&col01` のように、インターフェースの識別部分だけ）。"""
    low = (path or "").lower()
    for seg in low.split("#"):
        if seg.startswith("vid_") and "&mi_" in seg:
            return seg.split("&", 2)[-1] if seg.count("&") >= 2 else seg
        if seg.startswith("vid_"):
            return seg
    return low[-24:]


def parse_report(buf: bytes | bytearray | None, report_id: int = REPORT_ID_BUTTONS) -> str | None:
    """入力レポートを解釈する（純関数）。戻りは PRESS（`01 01…`）／RELEASE（`01 00…`）／None
    （空・短すぎる・別のレポート ID・未知のビット）。実測: 押す/離すは同時刻のパルス。"""
    if not buf or len(buf) < 2 or buf[0] != report_id:
        return None
    if buf[1] == 0x01:
        return PRESS
    if buf[1] == 0x00:
        return RELEASE
    return None


def caps_have_play_pause(caps: list[tuple[int, bool, int, int]]) -> bool:
    """入力の caps（(usage_page, is_range, usage_min, usage_max)。単独 usage は min=max）に、
    Consumer Page の Play/Pause（0xCD）が含まれるか。"""
    for page, _is_range, lo, hi in caps:
        if page == USAGE_PAGE_CONSUMER and lo <= USAGE_PLAY_PAUSE <= hi:
            return True
    return False


@dataclass
class HidInfo:
    """列挙したインターフェース1つの情報。"""
    path: str
    usage_page: int = 0
    usage: int = 0
    input_len: int = 0
    has_play_pause: bool | None = None   # caps で確認できたか（None=確認できなかった）


def rank_candidates(infos: list[HidInfo], vid_pid: str = DEFAULT_VID_PID) -> list[HidInfo]:
    """VID/PID が一致するものから、読む候補を順位づけして返す（純関数）。
    1) caps で Play/Pause（Consumer 0xCD）を確認できたもの  2) パス名に `col01` を含むもの（確認できなかった場合の保険）。
    どちらでもないもの（別のコレクション）は含めない。"""
    vp = parse_vid_pid(vid_pid)
    if vp is None:
        return []
    mine = [i for i in infos if path_matches(i.path, *vp)]
    first = [i for i in mine if i.has_play_pause is True]
    second = [i for i in mine if i.has_play_pause is not True and is_col01(i.path)]
    return first + second


# ---------------------------------------------------------------------------
# 結果・読み取りの部品

@dataclass
class HidStartResult:
    ok: bool
    reason: str = ""
    code: int = 0
    path: str = ""


class HidReadError(OSError):
    """読み取りの失敗（デバイスが抜けた等）。"""


class HidButtonReader:
    """ボタン1押しにつき on_press() を1回呼ぶ（専用スレッドから）。
    lister() -> list[HidInfo] / opener(info) -> source を差し替えるとテストできる。
    source: read_report() -> bytes | None（None=停止要求）, cancel()（別スレッドから呼べる）, close()。"""

    def __init__(self, vid_pid: str = DEFAULT_VID_PID, lister: Callable[[], list[HidInfo]] | None = None,
                 opener: Callable[[HidInfo], object] | None = None):
        self.vid_pid = vid_pid
        self._lister = lister
        self._opener = opener
        self._thread: threading.Thread | None = None
        self._source = None
        self._on_press: Callable[[], None] = lambda: None
        self._on_exit: Callable[[], None] = lambda: None
        self.exit_reason = ""
        self.device = ""            # 開いたインターフェースの短い名前（診断用）
        self.reports = 0            # 受け取ったレポートの数
        self.presses = 0            # 押下（01 01）の数
        self.last_report = ""       # 最後のレポートの先頭 8 バイト（16進）
        self._tele_source = None
        self._tele_thread: threading.Thread | None = None
        self.tele_device = ""
        self.tele_reports = 0       # 電話用インターフェースから受け取ったレポートの数（押下として数える）

    def diagnostics(self) -> dict:
        return {"device": self.device, "reports": self.reports, "presses": self.presses,
                "last_report": self.last_report, "tele_device": self.tele_device, "tele_reports": self.tele_reports}

    def thread_alive(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def start(self, on_press: Callable[[], None], on_exit: Callable[[], None], timeout: float = 3.0) -> HidStartResult:
        if self._lister is None and sys.platform != "win32":
            return HidStartResult(False, "Windows ではありません")
        if self.thread_alive():
            return HidStartResult(True, path=self.device)
        self._on_press, self._on_exit = on_press, on_exit
        self.exit_reason = ""
        self.reports = self.presses = 0
        self.last_report = ""
        vp = parse_vid_pid(self.vid_pid)
        if vp is None:
            return HidStartResult(False, f"ヘッドセットの ID（{self.vid_pid!r}）を読めません。例: 3511:2EF2")
        try:
            infos = self._lister() if self._lister else list_hid_infos(self.vid_pid)
        except Exception as e:
            return HidStartResult(False, f"HID デバイスの列挙に失敗: {type(e).__name__}: {e}")
        mine = [i for i in infos if path_matches(i.path, *vp)]
        if not mine:
            return HidStartResult(False, f"ヘッドセット（{self.vid_pid}）の HID デバイスが見つかりません（USB ドングルが挿さっていますか）")
        cands = rank_candidates(infos, self.vid_pid)
        if not cands:
            return HidStartResult(False, "ヘッドセットは見つかりましたが、再生/一時停止ボタンを持つ HID インターフェースがありません")
        last_err, code = "", 0
        for info in cands:
            try:
                src = (self._opener or open_source)(info)
            except HidReadError as e:
                last_err, code = str(e), getattr(e, "winerror", 0) or 0
                continue
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            self._source = src
            self.device = short_path(info.path)
            self._thread = threading.Thread(target=self._run, args=(src,), name="kabeuchi-hid", daemon=True)
            self._thread.start()
            self._start_telephony(mine)
            return HidStartResult(True, path=self.device)
        return HidStartResult(False, "HID デバイスを開けませんでした: " + (last_err or "不明"), code)

    def _start_telephony(self, mine: list) -> None:
        """マイクを使っている間（通話状態）は、多機能ボタンが、再生/一時停止ではなく電話用（Page 0x0B）のインターフェースに出る
        （実測）。そのインターフェースも読んで、レポートが来たら押下として扱う。開けなくても、本来の経路には影響しない。"""
        self.tele_device, self.tele_reports = "", 0
        for info in mine:
            if info.usage_page != USAGE_PAGE_TELEPHONY:
                continue
            try:
                src = (self._opener or open_source)(info)
            except Exception:
                continue
            self._tele_source = src
            self.tele_device = short_path(info.path)
            self._tele_thread = threading.Thread(target=self._run_telephony, args=(src,), name="kabeuchi-hid-tel", daemon=True)
            self._tele_thread.start()
            return

    def _run_telephony(self, src) -> None:
        try:
            while True:
                rep = src.read_report()
                if rep is None:
                    break
                if rep and rep[0] == REPORT_ID_TELEPHONY:
                    self.tele_reports += 1
                    try:
                        self._on_press()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                src.close()
            except Exception:
                pass
            self._tele_source = None

    def stop(self) -> None:
        src, t = self._source, self._thread
        tsrc, tt = self._tele_source, self._tele_thread
        if tsrc is not None:
            try:
                tsrc.cancel()
            except Exception:
                pass
        if tt is not None and tt is not threading.current_thread():
            tt.join(timeout=3)
        self._tele_thread = None
        if src is not None:
            try:
                src.cancel()
            except Exception:
                pass
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None

    def _run(self, src) -> None:
        try:
            while True:
                rep = src.read_report()
                if rep is None:
                    self.exit_reason = "停止要求"
                    break
                self.reports += 1
                self.last_report = bytes(rep[:8]).hex(" ")
                if parse_report(rep) == PRESS:
                    self.presses += 1
                    try:
                        self._on_press()
                    except Exception:
                        pass
        except Exception as e:
            self.exit_reason = f"読み取りが止まりました: {type(e).__name__}: {e}"
        finally:
            try:
                src.close()
            except Exception:
                pass
            self._source = None
            try:
                self._on_exit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Win32（ここから下は Windows でだけ使う。呼ぶまで DLL は読まない）

GENERIC_READ = 0x80000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
ERROR_IO_PENDING, ERROR_OPERATION_ABORTED, ERROR_NO_MORE_ITEMS = 997, 995, 259
WAIT_OBJECT_0, INFINITE = 0, 0xFFFFFFFF
HIDP_STATUS_SUCCESS = 0x00110000
HIDP_INPUT = 0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("InterfaceClassGuid", _GUID), ("Flags", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_uint16), ("UsagePage", ctypes.c_uint16),
                ("InputReportByteLength", ctypes.c_uint16), ("OutputReportByteLength", ctypes.c_uint16),
                ("FeatureReportByteLength", ctypes.c_uint16), ("Reserved", ctypes.c_uint16 * 17),
                ("NumberLinkCollectionNodes", ctypes.c_uint16),
                ("NumberInputButtonCaps", ctypes.c_uint16), ("NumberInputValueCaps", ctypes.c_uint16),
                ("NumberInputDataIndices", ctypes.c_uint16),
                ("NumberOutputButtonCaps", ctypes.c_uint16), ("NumberOutputValueCaps", ctypes.c_uint16),
                ("NumberOutputDataIndices", ctypes.c_uint16),
                ("NumberFeatureButtonCaps", ctypes.c_uint16), ("NumberFeatureValueCaps", ctypes.c_uint16),
                ("NumberFeatureDataIndices", ctypes.c_uint16)]


class _CapsRange(ctypes.Structure):
    _fields_ = [("UsageMin", ctypes.c_uint16), ("UsageMax", ctypes.c_uint16),
                ("StringMin", ctypes.c_uint16), ("StringMax", ctypes.c_uint16),
                ("DesignatorMin", ctypes.c_uint16), ("DesignatorMax", ctypes.c_uint16),
                ("DataIndexMin", ctypes.c_uint16), ("DataIndexMax", ctypes.c_uint16)]


class _CapsNotRange(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_uint16), ("Reserved1", ctypes.c_uint16),
                ("StringIndex", ctypes.c_uint16), ("Reserved2", ctypes.c_uint16),
                ("DesignatorIndex", ctypes.c_uint16), ("Reserved3", ctypes.c_uint16),
                ("DataIndex", ctypes.c_uint16), ("Reserved4", ctypes.c_uint16)]


class _CapsUnion(ctypes.Union):
    _fields_ = [("Range", _CapsRange), ("NotRange", _CapsNotRange)]


class _HIDP_BUTTON_CAPS(ctypes.Structure):      # 72 バイト
    _fields_ = [("UsagePage", ctypes.c_uint16), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", ctypes.c_uint16), ("LinkCollection", ctypes.c_uint16),
                ("LinkUsage", ctypes.c_uint16), ("LinkUsagePage", ctypes.c_uint16),
                ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte),
                ("Reserved", ctypes.c_uint32 * 10), ("U", _CapsUnion)]


class _HIDP_VALUE_CAPS(ctypes.Structure):       # 72 バイト
    _fields_ = [("UsagePage", ctypes.c_uint16), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", ctypes.c_uint16), ("LinkCollection", ctypes.c_uint16),
                ("LinkUsage", ctypes.c_uint16), ("LinkUsagePage", ctypes.c_uint16),
                ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte),
                ("HasNull", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte),
                ("BitSize", ctypes.c_uint16), ("ReportCount", ctypes.c_uint16),
                ("Reserved2", ctypes.c_uint16 * 5), ("UnitsExp", ctypes.c_uint32), ("Units", ctypes.c_uint32),
                ("LogicalMin", ctypes.c_int32), ("LogicalMax", ctypes.c_int32),
                ("PhysicalMin", ctypes.c_int32), ("PhysicalMax", ctypes.c_int32), ("U", _CapsUnion)]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32), ("hEvent", ctypes.c_void_p)]


_api_cache = None


def _api():
    """Win32 の関数（argtypes つき）を初回だけ用意する。共有の windll は書き換えない。"""
    global _api_cache
    if _api_cache is not None:
        return _api_cache
    from types import SimpleNamespace
    V, P, U32, U16 = ctypes.c_void_p, ctypes.POINTER, ctypes.c_uint32, ctypes.c_uint16
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    sa = ctypes.WinDLL("setupapi", use_last_error=True)
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, U32, U32, V, U32, U32, V]
    k32.CreateFileW.restype = V
    k32.CloseHandle.argtypes = [V]
    k32.CloseHandle.restype = ctypes.c_int
    k32.ReadFile.argtypes = [V, V, U32, P(U32), P(_OVERLAPPED)]
    k32.ReadFile.restype = ctypes.c_int
    k32.GetOverlappedResult.argtypes = [V, P(_OVERLAPPED), P(U32), ctypes.c_int]
    k32.GetOverlappedResult.restype = ctypes.c_int
    k32.CancelIoEx.argtypes = [V, P(_OVERLAPPED)]
    k32.CancelIoEx.restype = ctypes.c_int
    k32.CreateEventW.argtypes = [V, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    k32.CreateEventW.restype = V
    k32.SetEvent.argtypes = [V]
    k32.SetEvent.restype = ctypes.c_int
    k32.WaitForMultipleObjects.argtypes = [U32, P(V), ctypes.c_int, U32]
    k32.WaitForMultipleObjects.restype = U32
    hid.HidD_GetHidGuid.argtypes = [P(_GUID)]
    hid.HidD_GetHidGuid.restype = None
    hid.HidD_GetPreparsedData.argtypes = [V, P(V)]
    hid.HidD_GetPreparsedData.restype = ctypes.c_ubyte
    hid.HidD_FreePreparsedData.argtypes = [V]
    hid.HidD_FreePreparsedData.restype = ctypes.c_ubyte
    hid.HidP_GetCaps.argtypes = [V, P(_HIDP_CAPS)]
    hid.HidP_GetCaps.restype = ctypes.c_int32
    hid.HidP_GetButtonCaps.argtypes = [ctypes.c_int, P(_HIDP_BUTTON_CAPS), P(U16), V]
    hid.HidP_GetButtonCaps.restype = ctypes.c_int32
    hid.HidP_GetValueCaps.argtypes = [ctypes.c_int, P(_HIDP_VALUE_CAPS), P(U16), V]
    hid.HidP_GetValueCaps.restype = ctypes.c_int32
    sa.SetupDiGetClassDevsW.argtypes = [P(_GUID), ctypes.c_wchar_p, V, U32]
    sa.SetupDiGetClassDevsW.restype = V
    sa.SetupDiEnumDeviceInterfaces.argtypes = [V, V, P(_GUID), U32, P(_SP_DEVICE_INTERFACE_DATA)]
    sa.SetupDiEnumDeviceInterfaces.restype = ctypes.c_int
    sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [V, P(_SP_DEVICE_INTERFACE_DATA), V, U32, P(U32), V]
    sa.SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_int
    sa.SetupDiDestroyDeviceInfoList.argtypes = [V]
    sa.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int
    _api_cache = SimpleNamespace(k32=k32, hid=hid, sa=sa, INVALID=ctypes.c_void_p(-1).value)
    return _api_cache


def _enum_paths() -> list[str]:
    """HID デバイスインターフェースのパスを列挙する（SetupAPI。デバイスには触れない）。"""
    a = _api()
    guid = _GUID()
    a.hid.HidD_GetHidGuid(ctypes.byref(guid))
    hdev = a.sa.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev in (None, a.INVALID):
        raise HidReadError(f"SetupDiGetClassDevs 失敗（Win32 エラー {ctypes.get_last_error()}）")
    paths: list[str] = []
    try:
        i = 0
        while True:
            data = _SP_DEVICE_INTERFACE_DATA()
            data.cbSize = ctypes.sizeof(data)
            if not a.sa.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), i, ctypes.byref(data)):
                break    # ERROR_NO_MORE_ITEMS（それ以外のエラーでもここで打ち切る）
            i += 1
            need = ctypes.c_uint32(0)
            a.sa.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(data), None, 0, ctypes.byref(need), None)
            if need.value < 6:
                continue
            buf = ctypes.create_string_buffer(need.value)
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6   # cbSize
            if a.sa.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(data), buf, need.value, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        a.sa.SetupDiDestroyDeviceInfoList(hdev)
    return paths


def _open_handle(path: str, access: int, overlapped: bool):
    a = _api()
    flags = FILE_FLAG_OVERLAPPED if overlapped else 0
    h = a.k32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, flags, None)
    if h in (None, a.INVALID):
        err = ctypes.get_last_error()
        e = HidReadError(f"CreateFile 失敗（Win32 エラー {err}）")
        e.winerror = err
        raise e
    return h


def inspect_path(path: str) -> HidInfo:
    """パスのインターフェースを問い合わせ専用（アクセス 0・共有）で開き、caps を調べる。読み書きはしない。"""
    a = _api()
    info = HidInfo(path)
    h = _open_handle(path, 0, False)
    try:
        pre = ctypes.c_void_p()
        if not a.hid.HidD_GetPreparsedData(h, ctypes.byref(pre)):
            return info
        try:
            caps = _HIDP_CAPS()
            if a.hid.HidP_GetCaps(pre, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                return info
            info.usage_page, info.usage, info.input_len = caps.UsagePage, caps.Usage, caps.InputReportByteLength
            found: list[tuple[int, bool, int, int]] = []
            for struct_t, getter, n in ((_HIDP_BUTTON_CAPS, a.hid.HidP_GetButtonCaps, caps.NumberInputButtonCaps),
                                        (_HIDP_VALUE_CAPS, a.hid.HidP_GetValueCaps, caps.NumberInputValueCaps)):
                if n <= 0:
                    continue
                arr = (struct_t * n)()
                cnt = ctypes.c_uint16(n)
                if getter(HIDP_INPUT, arr, ctypes.byref(cnt), pre) != HIDP_STATUS_SUCCESS:
                    continue
                for c in arr[:cnt.value]:
                    if c.IsRange:
                        found.append((c.UsagePage, True, c.U.Range.UsageMin, c.U.Range.UsageMax))
                    else:
                        found.append((c.UsagePage, False, c.U.NotRange.Usage, c.U.NotRange.Usage))
            info.has_play_pause = caps_have_play_pause(found)
        finally:
            a.hid.HidD_FreePreparsedData(pre)
    finally:
        a.k32.CloseHandle(h)
    return info


def list_hid_infos(vid_pid: str = DEFAULT_VID_PID) -> list[HidInfo]:
    """指定 VID/PID の HID インターフェースの情報。他のデバイスは開かない（列挙してパス名で絞るだけ）。
    問い合わせに失敗したものは caps なしで含める（パス名での絞り込みが効く）。"""
    vp = parse_vid_pid(vid_pid)
    out: list[HidInfo] = []
    for p in _enum_paths():
        if vp is None or not path_matches(p, *vp):
            continue
        try:
            out.append(inspect_path(p))
        except Exception:
            out.append(HidInfo(p))
    return out


class _OverlappedSource:
    """overlapped ReadFile の読み取り元。read_report() は専用スレッドで、cancel() は別スレッドから。"""

    def __init__(self, path: str, input_len: int):
        a = self._a = _api()
        self.handle = _open_handle(path, GENERIC_READ, True)
        self.len = int(input_len) if input_len and input_len > 0 else DEFAULT_INPUT_LEN
        self._stop = a.k32.CreateEventW(None, 1, 0, None)        # 手動リセット
        self._done = a.k32.CreateEventW(None, 1, 0, None)
        self._closed = False

    def cancel(self) -> None:
        if self._stop:
            self._a.k32.SetEvent(self._stop)

    def read_report(self) -> bytes | None:
        a, k = self._a, self._a.k32
        handles = (ctypes.c_void_p * 2)(self._done, self._stop)
        if k.WaitForMultipleObjects(1, (ctypes.c_void_p * 1)(self._stop), 0, 0) == WAIT_OBJECT_0:
            return None                                           # 停止要求が先に来ていた
        buf = ctypes.create_string_buffer(self.len)
        ov = _OVERLAPPED()
        ov.hEvent = self._done
        got = ctypes.c_uint32(0)
        ok = k.ReadFile(self.handle, buf, self.len, None, ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                e = HidReadError(f"ReadFile 失敗（Win32 エラー {err}）")
                e.winerror = err
                raise e
            r = k.WaitForMultipleObjects(2, handles, 0, INFINITE)
            if r != WAIT_OBJECT_0:                                # 停止要求（または待ちの失敗）
                k.CancelIoEx(self.handle, ctypes.byref(ov))
                k.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(got), 1)   # 完了を待つ（buf を安全に手放す）
                return None
        if not k.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(got), 0):
            err = ctypes.get_last_error()
            if err == ERROR_OPERATION_ABORTED:
                return None
            e = HidReadError(f"読み取りの結果取得に失敗（Win32 エラー {err}）")
            e.winerror = err
            raise e
        return buf.raw[:got.value]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        k = self._a.k32
        for h in (self.handle, self._stop, self._done):
            try:
                if h:
                    k.CloseHandle(h)
            except Exception:
                pass


def open_source(info: HidInfo):
    """読み取り元を開く（GENERIC_READ・共有・overlapped）。開けなければ HidReadError。"""
    return _OverlappedSource(info.path, info.input_len)
