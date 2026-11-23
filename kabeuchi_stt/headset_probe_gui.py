# -*- coding: utf-8 -*-
"""ヘッドセットボタン診断ツール（コダマ付属・単独で動く小さな窓）。

目的: ヘッドセット（Shokz Loop120 など）の多機能ボタンが、PC のどの経路で届いているかを自分で試して見る。
  経路A  HID 直接読み（dialogue_hid。読み取り専用・生のレポートを16進で表示）
  経路B  再生/一時停止キー（RegisterHotKey。独占すると他アプリの再生/一時停止が効かなくなる）
  経路C  キーボードのメディアキー（WH_KEYBOARD_LL。押下の検出だけで、握りつぶさない・参考）
  さらに本体と同じ HeadsetButton（auto）に同じ押下を流し、「コダマなら確定と受け取るか」を1行で出す。

安全: 起動しただけでは何も監視しない（［開始］か Enter で開始）。終了・窓を閉じるときに必ず解放する。
ヘッドセットへ何も書かない・キーもマウスも送らない。クリップボードへは自動で入れない。
注意: コダマ本体でセッション中は、本体のヘッドセット検出と取り合いになる。
"""
from __future__ import annotations

import atexit
import ctypes
import html
import os
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dialogue_hid as D  # noqa: E402
import headset_probe_logic as L  # noqa: E402

TITLE = "ヘッドセットボタン診断"
LOG_DIR = HERE / "stage0" / "probe_logs"
MAX_LOG_LINES = 2000

VK_MEDIA = {0xB3: "再生/一時停止", 0xAF: "音量＋", 0xAE: "音量－", 0xB0: "次の曲", 0xB1: "前の曲"}
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT = 0x0100, 0x0104, 0x0012
LLKHF_INJECTED = 0x10


# ---------------------------------------------------------------------------
# Win32 の部品（呼ぶまで何も触らない）

def _product_name(path: str) -> str:
    """製品名（取れなければ空）。問い合わせ専用（アクセス 0）で開くだけ。"""
    try:
        a = D._api()
        f = a.hid.HidD_GetProductString
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        f.restype = ctypes.c_ubyte
        h = D._open_handle(path, 0, False)
        try:
            buf = ctypes.create_unicode_buffer(128)
            if f(h, buf, ctypes.sizeof(buf)):
                return buf.value.strip()
        finally:
            a.k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def list_all_devices() -> list[L.ProbeDevice]:
    """接続中の HID インターフェース全部（列挙して、問い合わせ専用で caps を見るだけ。読み書きはしない）。"""
    out: list[L.ProbeDevice] = []
    for p in D._enum_paths():
        try:
            info = D.inspect_path(p)
        except Exception:
            info = D.HidInfo(p)
        out.append(L.device_from_info(info, _product_name(p)))
    return L.sort_devices(out)


class _TapSource:
    """HID の読み取り元をラップして、受け取ったレポートを on_report へ渡す（読むだけ・素通し）。"""

    def __init__(self, inner, on_report):
        self.inner, self.on_report = inner, on_report

    def read_report(self):
        rep = self.inner.read_report()
        if rep is not None:
            try:
                self.on_report(bytes(rep))
            except Exception:
                pass
        return rep

    def cancel(self):
        self.inner.cancel()

    def close(self):
        self.inner.close()


class _TapBackend:
    """ホットキーのバックエンドをラップして、WM_HOTKEY の受信を on_raw にも知らせる。"""

    def __init__(self, inner, on_raw):
        self._inner, self._on_raw = inner, on_raw

    def start(self, on_press, on_exit, timeout: float = 3.0):
        def press():
            try:
                self._on_raw()
            except Exception:
                pass
            on_press()
        return self._inner.start(press, on_exit, timeout)

    def stop(self):
        self._inner.stop()

    def __getattr__(self, name):          # thread_alive / exit_reason など
        return getattr(self._inner, name)


class MediaKeyHook:
    """WH_KEYBOARD_LL で、メディアキーの押下を検出するだけ（素通し）。コールバックはキューに積むだけ。
    受け取り側スレッドが on_key(vk, injected, t) を呼ぶ。"""

    def __init__(self, on_key):
        self._on_key = on_key
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = None
        self._fwd = None
        self._tid = 0
        self._ready = threading.Event()
        self._result = (False, "未開始")
        self._proc = None

    def start(self, timeout: float = 3.0) -> tuple[bool, str]:
        if sys.platform != "win32":
            return False, "Windows ではありません"
        if self._thread is not None and self._thread.is_alive():
            return True, ""
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="probe-keyhook", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            return False, "フックの応答がありません（タイムアウト）"
        if self._result[0]:
            self._fwd = threading.Thread(target=self._forward, name="probe-keyfwd", daemon=True)
            self._fwd.start()
            atexit.register(self.stop)
        return self._result

    def stop(self) -> None:
        t, tid = self._thread, self._tid
        if t is not None and tid:
            try:
                from ctypes import wintypes
                u32 = ctypes.WinDLL("user32", use_last_error=True)
                u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                u32.PostThreadMessageW.restype = wintypes.BOOL
                u32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
            if t is not threading.current_thread():
                t.join(timeout=3)
        f = self._fwd
        if f is not None:
            self._q.put(None)
            if f is not threading.current_thread():
                f.join(timeout=2)
        self._thread = self._fwd = None
        self._tid = 0
        try:
            atexit.unregister(self.stop)
        except Exception:
            pass

    def _forward(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                self._on_key(*item)
            except Exception:
                pass

    def _run(self) -> None:
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
        u32.SetWindowsHookExW.restype = ctypes.c_void_p
        u32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u32.CallNextHookEx.restype = ctypes.c_ssize_t
        u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u32.UnhookWindowsHookEx.restype = wintypes.BOOL
        u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u32.GetMessageW.restype = ctypes.c_int
        u32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        u32.PeekMessageW.restype = wintypes.BOOL
        k32.GetCurrentThreadId.restype = wintypes.DWORD
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        q = self._q
        vks = VK_MEDIA

        def proc(n_code, w_param, l_param):
            try:       # 最小限: メディアキーの押下だけ、キューに積む。握りつぶさない
                if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    st = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if st.vkCode in vks:
                        q.put_nowait((int(st.vkCode), bool(st.flags & LLKHF_INJECTED), time.time()))
            except Exception:
                pass
            return u32.CallNextHookEx(None, n_code, w_param, l_param)

        self._proc = HOOKPROC(proc)      # 参照を保持（GC で消えるとクラッシュする）
        hook = None
        msg = wintypes.MSG()
        try:
            u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            self._tid = int(k32.GetCurrentThreadId())
            hook = u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, k32.GetModuleHandleW(None), 0)
            if hook:
                self._result = (True, "")
            else:
                self._result = (False, f"フックを入れられませんでした（Win32 エラー {ctypes.get_last_error()}）")
        except Exception as e:
            self._result = (False, f"フックで例外: {type(e).__name__}: {e}")
        finally:
            self._ready.set()
        if not hook:
            return
        try:
            while True:
                r = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:
                    break
        finally:
            try:
                u32.UnhookWindowsHookEx(hook)       # 必ず解除（入れたスレッドで）
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 画面

from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QFont, QKeySequence, QShortcut  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,  # noqa: E402
                               QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

GREEN = "#2e9e4f"
GRAY = "#8a8a8a"
RED = "#d04040"


class Bridge(QObject):
    """別スレッドの出来事を、窓のスレッドへ運ぶ（キューつき接続。待機中は何も走らない）。"""
    ev = Signal(str, object)


class StatusRow(QFrame):
    """経路ごとの状態1行。押した瞬間に緑で点滅し、最後の反応と回数を出す。"""

    def __init__(self, key: str, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("row_" + key)
        self.setFrameShape(QFrame.StyledPanel)
        self.count = 0
        self.dot = QLabel("●")
        self.dot.setFixedWidth(22)
        self.title = QLabel(f"<b>{html.escape(title)}</b>")
        self.title.setMinimumWidth(190)
        self.status = QLabel("停止中")
        self.status.setWordWrap(True)
        self.last = QLabel("最後の反応 --:--:--.---")
        self.cnt = QLabel("回数 0")
        self.cnt.setMinimumWidth(56)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.addWidget(self.dot)
        lay.addWidget(self.title)
        lay.addWidget(self.status, 1)
        lay.addWidget(self.last)
        lay.addWidget(self.cnt)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._unflash)
        self._color = GRAY
        self._paint(GRAY)

    def _paint(self, color: str) -> None:
        self.dot.setStyleSheet(f"color:{color};font-size:16px;")

    def set_state(self, text: str, level: str = "idle") -> None:
        """level: idle（灰）/ ok（緑）/ ng（赤）"""
        self._color = {"ok": GREEN, "ng": RED}.get(level, GRAY)
        self.status.setText(text)
        self._paint(self._color)

    def reset(self, text: str = "停止中") -> None:
        self.count = 0
        self.cnt.setText("回数 0")
        self.last.setText("最後の反応 --:--:--.---")
        self.set_state(text, "idle")

    def hit(self, t: float, counted: bool = True) -> None:
        if counted:
            self.count += 1
            self.cnt.setText(f"回数 {self.count}")
        self.last.setText("最後の反応 " + L.fmt_time(t))
        self.setStyleSheet(f"#{self.objectName()} {{ background: rgba(46,158,79,70); }}")
        self._paint(GREEN)
        self._timer.start()

    def _unflash(self) -> None:
        self.setStyleSheet("")
        self._paint(self._color)


class ProbeWindow(QWidget):
    def __init__(self, lister=None, auto_scan: bool = True):
        super().__init__()
        self.setWindowTitle(TITLE)
        self.resize(720, 760)
        self._lister = lister or list_all_devices
        self.stats = L.ProbeStats()
        self.tracker = L.ReportTracker()
        self.bridge = Bridge()
        self.bridge.ev.connect(self._on_event, Qt.QueuedConnection)
        self.hb = None
        self.keyhook: MediaKeyHook | None = None
        self.devices: list[L.ProbeDevice] = []
        self._build()
        self._refresh_summary()
        if auto_scan:
            QTimer.singleShot(0, self.rescan)

    # ---- 組み立て
    def _build(self) -> None:
        root = QVBoxLayout(self)
        warn = QLabel("コダマ本体でセッション中は、本体のヘッドセット検出と取り合いになります。"
                      "診断の前に、本体のセッションを終えてください。")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{RED};font-weight:bold;")
        root.addWidget(warn)

        dev = QHBoxLayout()
        dev.addWidget(QLabel("HID デバイス"))
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(40)
        dev.addWidget(self.combo, 1)
        self.btn_rescan = QPushButton("再検索")
        self.btn_rescan.clicked.connect(self.rescan)
        dev.addWidget(self.btn_rescan)
        root.addLayout(dev)

        opt = QHBoxLayout()
        self.cb_excl = QCheckBox("独占する（ホットキー登録）")
        self.cb_excl.setChecked(True)
        opt.addWidget(self.cb_excl)
        opt.addWidget(QLabel("独占すると、ほかのアプリの再生/一時停止が効かなくなります"))
        opt.addStretch(1)
        root.addLayout(opt)
        opt2 = QHBoxLayout()
        self.cb_keys = QCheckBox("メディアキーも見る（参考。キーは握りつぶさない）")
        self.cb_keys.setChecked(True)
        opt2.addWidget(self.cb_keys)
        opt2.addStretch(1)
        root.addLayout(opt2)

        btns = QHBoxLayout()
        self.btn_start = QPushButton("開始（Enter）")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        self.state_label = QLabel("停止中（何も監視していません）")
        btns.addWidget(self.btn_start)
        btns.addWidget(self.btn_stop)
        btns.addWidget(self.state_label, 1)
        root.addLayout(btns)
        for key in (Qt.Key_Return, Qt.Key_Enter):
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(self._enter)

        self.row_a = StatusRow("a", "A  HID 直接読み")
        self.row_b = StatusRow("b", "B  再生/一時停止キー（ホットキー）")
        self.row_c = StatusRow("c", "C  キーボードのメディアキー（参考）")
        for r in (self.row_a, self.row_b, self.row_c):
            root.addWidget(r)

        self.kodama = QLabel("コダマの判定: （押下待ち）")
        self.kodama.setWordWrap(True)
        self.kodama.setStyleSheet("font-weight:bold;")
        root.addWidget(self.kodama)

        root.addWidget(QLabel("<b>診断のまとめ</b>"))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.summary.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.summary.setMinimumHeight(96)
        root.addWidget(self.summary)

        lh = QHBoxLayout()
        lh.addWidget(QLabel("<b>ログ</b>"))
        lh.addStretch(1)
        self.btn_save = QPushButton("ログを保存")
        self.btn_save.clicked.connect(self.save_log)
        self.btn_clear = QPushButton("ログをクリア")
        self.btn_clear.clicked.connect(self.clear_log)
        lh.addWidget(self.btn_save)
        lh.addWidget(self.btn_clear)
        root.addLayout(lh)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LOG_LINES)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        root.addWidget(self.log, 1)
        self.btn_start.setFocus()

    # ---- ログ
    def _append(self, body_html: str, t: float | None = None) -> None:
        ts = L.fmt_time(time.time() if t is None else t)
        self.log.appendHtml(f'<span style="color:{GRAY}">{ts}</span> {body_html}')

    def _note(self, text: str, t: float | None = None) -> None:
        self._append(html.escape(text), t)

    def clear_log(self) -> None:
        self.log.clear()

    def log_text(self) -> str:
        return self.log.toPlainText()

    def save_log(self, path: str | None = None) -> str | None:
        if not path:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            default = str(LOG_DIR / ("headset_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"))
            path, _ = QFileDialog.getSaveFileName(self, "ログを保存", default, "テキスト (*.txt)")
            if not path:
                return None
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(self.log_text() + "\n", encoding="utf-8")
        except Exception as e:
            self._note(f"ログを保存できませんでした: {type(e).__name__}: {e}")
            return None
        self._note(f"ログを保存しました: {path}")
        return path

    # ---- デバイス
    def rescan(self) -> None:
        try:
            devs = list(self._lister())
        except Exception as e:
            devs = []
            self._note(f"HID デバイスの列挙に失敗: {type(e).__name__}: {e}")
        self.devices = devs
        self.combo.clear()
        for d in devs:
            self.combo.addItem(L.device_label(d), d)
        i = L.pick_default(devs)
        if i >= 0:
            self.combo.setCurrentIndex(i)
        self._note(f"HID デバイス {len(devs)} 件" + ("" if devs else "（見つかりません）"))

    def _selected(self) -> L.ProbeDevice | None:
        return self.combo.currentData() if self.combo.count() else None

    # ---- 開始・停止
    def _enter(self) -> None:
        if not self.stats.monitoring:
            self.start()

    @property
    def running(self) -> bool:
        return self.stats.monitoring

    def start(self) -> None:
        if self.stats.monitoring:
            return
        from dialogue_headset import HeadsetButton, _Win32Backend
        from dialogue_hid import HidButtonReader
        self.tracker.reset()
        s = self.stats = L.ProbeStats(started=True, monitoring=True)
        for r in (self.row_a, self.row_b, self.row_c):
            r.reset("開始しています…")
        self.kodama.setText("コダマの判定: （押下待ち）")
        dev = self._selected()
        excl = self.cb_excl.isChecked()
        self._note("監視を開始します")
        emit = self.bridge.ev.emit

        # コダマ本体と同じ HeadsetButton（auto 相当）。コントローラ・配達は呼ばない（押下通知を受けるだけ）
        hid_backend = hot_backend = None
        if dev is not None:
            info = dev.to_info()
            hid_backend = HidButtonReader(
                dev.vid_pid, lister=lambda: [info],
                opener=lambda i: _TapSource(D.open_source(i), lambda rep: self._on_report(rep)))
        if excl:
            hot_backend = _TapBackend(_Win32Backend(), lambda: emit("hotkey", time.time()))
        mode = "auto" if (hid_backend and hot_backend) else "hid" if hid_backend else "hotkey" if hot_backend else ""

        def hook(event, **f):                       # HeadsetButton の診断ログ → 判定の表示（dialogue.log へは書かない）
            if event == "headset_press":
                emit("verdict", {"via": f.get("via"), "accepted": bool(f.get("accepted")),
                                 "why": f.get("why", ""), "t": time.time()})

        s.a_enabled, s.b_enabled = hid_backend is not None, hot_backend is not None
        if not s.a_enabled:
            s.a_reason = "デバイスが選ばれていません"
        self.hb = None
        if mode:
            self.hb = HeadsetButton(lambda: None, mode=mode, backend=hot_backend, hid_backend=hid_backend,
                                    log=hook, vid_pid=(dev.vid_pid if dev else D.DEFAULT_VID_PID))
            try:
                self.hb.start()
            except Exception as e:
                self._note(f"HeadsetButton の開始で例外: {type(e).__name__}: {e}")
            paths = self.hb.diagnostics()["paths"]
            pa, pb = paths.get(D and "hid", {}), paths.get("hotkey", {})
            if s.a_enabled:
                s.a_ok, s.a_reason = bool(pa.get("ok")), pa.get("reason", "")
            if s.b_enabled:
                s.b_ok, s.b_code = bool(pb.get("ok")), int(pb.get("code") or 0)
                s.b_reason = "" if s.b_ok else (L.hotkey_error_text(s.b_code) if s.b_code else pb.get("reason", ""))
        # 経路C
        s.c_enabled = self.cb_keys.isChecked()
        self.keyhook = None
        if s.c_enabled:
            self.keyhook = MediaKeyHook(lambda vk, inj, t: emit("mediakey", {"vk": vk, "inj": inj, "t": t}))
            ok, reason = self.keyhook.start()
            s.c_ok, s.c_reason = ok, reason
        self._show_start_states()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        for w in (self.combo, self.btn_rescan, self.cb_excl, self.cb_keys):
            w.setEnabled(False)
        self.state_label.setText("監視中。多機能ボタンを1回だけ押してください")
        self._refresh_summary()

    def _show_start_states(self) -> None:
        s = self.stats
        if not s.a_enabled:
            self.row_a.set_state("監視していません（" + s.a_reason + "）", "idle")
        elif s.a_ok:
            self.row_a.set_state("読み取り中（" + (self._selected() and D.short_path(self._selected().path) or "") + "）", "ok")
            self._note("経路A: HID を開きました（読み取り専用）")
        else:
            self.row_a.set_state("開けませんでした: " + s.a_reason, "ng")
            self._note("経路A: 開けませんでした: " + s.a_reason)
        if not s.b_enabled:
            self.row_b.set_state("登録していません（独占しない）", "idle")
        elif s.b_ok:
            self.row_b.set_state("登録できました（独占中）", "ok")
            self._note("経路B: ホットキーを登録しました（独占中）")
        else:
            self.row_b.set_state("登録失敗: " + s.b_reason, "ng")
            self._note("経路B: 登録失敗: " + s.b_reason)
        if not s.c_enabled:
            self.row_c.set_state("監視していません", "idle")
        elif s.c_ok:
            self.row_c.set_state("監視中（握りつぶしません）", "ok")
            self._note("経路C: キーボードフックを入れました（素通し）")
        else:
            self.row_c.set_state("開始できませんでした: " + s.c_reason, "ng")
            self._note("経路C: 開始できませんでした: " + s.c_reason)

    def stop(self) -> None:
        was = self.stats.monitoring
        for obj in (self.hb, self.keyhook):
            if obj is not None:
                try:
                    obj.stop()
                except Exception:
                    pass
        self.hb = None
        self.keyhook = None
        self.stats.monitoring = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        for w in (self.combo, self.btn_rescan, self.cb_excl, self.cb_keys):
            w.setEnabled(True)
        self.state_label.setText("停止中（何も監視していません）")
        if was:
            self._note("監視を止めました（フック・ハンドルを解放）")
            for r in (self.row_a, self.row_b, self.row_c):
                r.status.setText(r.status.text() + "（停止）")
                r.set_state(r.status.text(), "idle")
        self._refresh_summary()

    def closeEvent(self, e):
        self.stop()
        super().closeEvent(e)

    # ---- 別スレッドから（シグナルへ積むだけ）
    def _on_report(self, rep: bytes) -> None:
        self.stats.a_reports += 1          # 整数の加算のみ（表示は窓のスレッドで）
        ev = self.tracker.feed(rep)
        if ev is not None:
            self.bridge.ev.emit("report", {"ev": ev, "t": time.time()})

    # ---- 窓のスレッドで
    def _on_event(self, kind: str, data) -> None:
        if not self.stats.monitoring:
            return
        s = self.stats
        if kind == "report":
            ev: L.ReportEvent = data["ev"]
            t = data["t"]
            if ev.kind == L.KIND_PRESS:
                s.a_presses += 1
            elif ev.kind == L.KIND_RELEASE:
                s.a_releases += 1
            if ev.suppressed_before:
                self._note(f"[A] （同一レポートを {ev.suppressed_before} 件省略）", t)
            self._append(f"[A] {L.hex_html(ev)}  <b>{html.escape(L.KIND_LABEL[ev.kind])}</b>  "
                         f"{html.escape(L.change_text(ev))}", t)
            self.row_a.hit(t, counted=ev.kind == L.KIND_PRESS)
        elif kind == "hotkey":
            s.b_presses += 1
            self._note("[B] WM_HOTKEY（再生/一時停止キー）を受信", data)
            self.row_b.hit(data)
        elif kind == "mediakey":
            vk = data["vk"]
            s.c_counts[vk] = s.c_counts.get(vk, 0) + 1
            self._note(f"[C] メディアキー押下: {VK_MEDIA.get(vk, hex(vk))}" + ("（注入されたキー）" if data["inj"] else ""), data["t"])
            self.row_c.hit(data["t"])
        elif kind == "verdict":
            v = L.verdict_of(data["accepted"], data["why"])
            if v == "ACCEPT":
                s.k_accept += 1
            elif v == "DUP":
                s.k_dup += 1
            elif v == "DROP":
                s.k_drop += 1
            via = data["via"]
            txt = {"ACCEPT": "確定として受け取る", "DUP": "重複（別経路が受け付け済み）",
                   "DROP": "間引き（直前の押下に近すぎる）"}.get(v, "受け取らない: " + data["why"])
            self.kodama.setText(f"コダマの判定: via={via} {v} … {txt}　"
                                f"（確定 {s.k_accept} / 重複 {s.k_dup} / 間引き {s.k_drop}）")
            self._note(f"[K] via={via} {v}: {txt}", data["t"])
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        self.summary.setText("\n".join(L.build_summary(self.stats)))


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    w = ProbeWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
