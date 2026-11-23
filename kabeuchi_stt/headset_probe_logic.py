# -*- coding: utf-8 -*-
"""ヘッドセットボタン診断ツール（headset_probe_gui）の純ロジック。Qt/Win32 非依存（テスト可能）。

- レポートの差分・押す/離すの推定（ReportTracker。連続する同一レポートは間引く）
- 16進表示（変化したバイトを強調した HTML）
- HID デバイスの表示名・既定の選び方
- ホットキー失敗の説明、診断のまとめ文
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field

import dialogue_hid as D

DEFAULT_VID_PID = D.DEFAULT_VID_PID
MAX_SHOW = 64          # ログに出すバイト数の上限
MIN_SHOW = 8

KIND_PRESS, KIND_RELEASE, KIND_CHANGE, KIND_NONE = "press", "release", "change", "none"
KIND_LABEL = {KIND_PRESS: "押す(推定)", KIND_RELEASE: "離す(推定)", KIND_CHANGE: "変化", KIND_NONE: "変化なし"}

HIGHLIGHT_STYLE = "color:#e03030;font-weight:bold"


def fmt_time(t: float) -> str:
    """HH:MM:SS.mmm"""
    ms = int((t - int(t)) * 1000)
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{ms:03d}"


# ---------------------------------------------------------------------------
# レポートの差分

@dataclass
class ReportEvent:
    raw: bytes                       # 受け取ったレポート全体
    length: int
    report_id: int
    prev: bytes                      # 比較に使った直前の同じ ID のレポート（無ければ 0 埋め）
    changed: list[int] = field(default_factory=list)   # 変化したバイトの位置（先頭のレポート ID は除く）
    kind: str = KIND_NONE
    kodama_view: str | None = None   # コダマ（dialogue_hid.parse_report）の解釈: press / release / None
    suppressed_before: int = 0       # この直前に間引いた「同一レポート」の数


def _at(b: bytes, i: int) -> int:
    return b[i] if i < len(b) else 0


def estimate_kind(prev: bytes, cur: bytes, changed: list[int]) -> str:
    """ビットが 0→1 に変わった＝押す、1→0＝離す、両方＝変化、無し＝変化なし（推定）。"""
    if not changed:
        return KIND_NONE
    up = down = False
    for i in changed:
        a, b = _at(prev, i), _at(cur, i)
        if b & ~a & 0xFF:
            up = True
        if a & ~b & 0xFF:
            down = True
    if up and not down:
        return KIND_PRESS
    if down and not up:
        return KIND_RELEASE
    return KIND_CHANGE


class ReportTracker:
    """入力レポートを1つずつ feed する。同じ ID のレポート同士で差分を取る（先頭バイトはレポート ID）。
    直前と完全に同一のレポートは None を返して間引く（間引いた数は次のイベントの suppressed_before へ）。"""

    def __init__(self) -> None:
        self._last: bytes | None = None
        self._by_id: dict[int, bytes] = {}
        self._suppressed = 0
        self.total = 0           # 受け取ったレポートの総数（間引き含む）
        self.suppressed_total = 0

    def reset(self) -> None:
        self.__init__()

    def feed(self, buf) -> ReportEvent | None:
        if not buf:
            return None
        raw = bytes(buf)
        self.total += 1
        if raw == self._last:
            self._suppressed += 1
            self.suppressed_total += 1
            return None
        self._last = raw
        rid = raw[0]
        prev = self._by_id.get(rid) or bytes(len(raw))
        n = max(len(raw), len(prev))
        changed = [i for i in range(1, n) if _at(raw, i) != _at(prev, i)]
        self._by_id[rid] = raw
        kind = estimate_kind(prev, raw, changed)
        view = D.parse_report(raw)
        if view in (D.PRESS, D.RELEASE):
            kind = KIND_PRESS if view == D.PRESS else KIND_RELEASE
        ev = ReportEvent(raw, len(raw), rid, prev, changed, kind, view, self._suppressed)
        self._suppressed = 0
        return ev


def shown_length(ev: ReportEvent) -> int:
    last = 1
    for i in range(len(ev.raw) - 1, -1, -1):
        if ev.raw[i]:
            last = i + 1
            break
    if ev.changed:
        last = max(last, max(ev.changed) + 1)
    return min(len(ev.raw), max(MIN_SHOW, last), MAX_SHOW)


def hex_plain(ev: ReportEvent) -> str:
    n = shown_length(ev)
    s = " ".join(f"{b:02x}" for b in ev.raw[:n])
    return s + (f" …(全{ev.length}バイト)" if ev.length > n else "")


def hex_html(ev: ReportEvent) -> str:
    """変化したバイトを赤・太字にした 16進（コピーしたときは普通の文字になる）。"""
    n = shown_length(ev)
    ch = set(ev.changed)
    parts = []
    for i in range(n):
        t = f"{ev.raw[i]:02x}"
        parts.append(f'<span style="{HIGHLIGHT_STYLE}">{t}</span>' if i in ch else t)
    s = " ".join(parts)
    if ev.length > n:
        s += html.escape(f" …(全{ev.length}バイト)")
    return s


def change_text(ev: ReportEvent) -> str:
    """変化の説明（例: `[1] 00→01`）。"""
    if not ev.changed:
        return "変化したバイトなし"
    shown = ev.changed[:8]
    s = " ".join(f"[{i}] {_at(ev.prev, i):02x}→{_at(ev.raw, i):02x}" for i in shown)
    if len(ev.changed) > len(shown):
        s += f" …他{len(ev.changed) - len(shown)}か所"
    return s


# ---------------------------------------------------------------------------
# デバイス

_VIDPID_RE = re.compile(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", re.I)


def vid_pid_from_path(path: str) -> str:
    m = _VIDPID_RE.search(path or "")
    return f"{m.group(1).upper()}:{m.group(2).upper()}" if m else "????:????"


@dataclass
class ProbeDevice:
    path: str
    vid_pid: str = ""
    name: str = ""
    usage_page: int = 0
    usage: int = 0
    input_len: int = 0
    has_play_pause: bool | None = None

    def to_info(self) -> D.HidInfo:
        """読み取り用の HidInfo。選んだデバイスは必ず読む（Play/Pause 判定は True に固定）。"""
        return D.HidInfo(self.path, self.usage_page, self.usage, self.input_len, True)


def device_from_info(info: D.HidInfo, name: str = "") -> ProbeDevice:
    return ProbeDevice(info.path, vid_pid_from_path(info.path), name, info.usage_page, info.usage,
                       info.input_len, info.has_play_pause)


def device_label(d: ProbeDevice) -> str:
    s = f"{d.vid_pid}  {D.short_path(d.path)}  Page 0x{d.usage_page:02X}/Usage 0x{d.usage:02X}  入力 {d.input_len}B"
    if d.has_play_pause:
        s += "  [再生/一時停止あり]"
    if d.name:
        s += f"  {d.name}"
    return s


def sort_devices(devs: list[ProbeDevice], vid_pid: str = DEFAULT_VID_PID) -> list[ProbeDevice]:
    """既定の VID:PID を先頭に、その中で Play/Pause を持つもの→col01→その他。残りは VID:PID・パス順。"""
    want = vid_pid.upper()

    def key(d: ProbeDevice):
        mine = d.vid_pid.upper() == want
        return (0 if mine else 1,
                0 if d.has_play_pause else 1,
                0 if D.is_col01(d.path) else 1,
                d.vid_pid, d.path)
    return sorted(devs, key=key)


def pick_default(devs: list[ProbeDevice], vid_pid: str = DEFAULT_VID_PID) -> int:
    """既定で選ぶ位置。既定の VID:PID が無ければ最初の候補（0）。空なら -1。"""
    if not devs:
        return -1
    ordered = sort_devices(devs, vid_pid)
    return devs.index(ordered[0]) if ordered[0] in devs else 0


# ---------------------------------------------------------------------------
# ホットキー・診断

def hotkey_error_text(code: int) -> str:
    if code == 1409:
        return ("他のアプリがすでに再生/一時停止キーを使っています（Win32 エラー 1409）。"
                "音楽・動画プレーヤーやメディア連携ツールを終了するか、「独占する」を切ってください")
    return f"登録できませんでした（Win32 エラー {code}）"


MEDIA_NAMES = {0xB3: "再生/一時停止", 0xAF: "音量＋", 0xAE: "音量－", 0xB0: "次の曲", 0xB1: "前の曲"}


@dataclass
class ProbeStats:
    started: bool = False            # 一度でも開始した
    monitoring: bool = False
    # 経路A
    a_enabled: bool = False
    a_ok: bool | None = None
    a_reason: str = ""
    a_reports: int = 0
    a_presses: int = 0
    a_releases: int = 0
    # 経路B
    b_enabled: bool = False
    b_ok: bool | None = None
    b_reason: str = ""
    b_code: int = 0
    b_presses: int = 0
    # 経路C
    c_enabled: bool = False
    c_ok: bool | None = None
    c_reason: str = ""
    c_counts: dict = field(default_factory=dict)   # vk -> 回数
    # コダマの判定
    k_accept: int = 0
    k_dup: int = 0
    k_drop: int = 0

    @property
    def c_total(self) -> int:
        return sum(self.c_counts.values())


def verdict_of(accepted: bool, why: str = "") -> str:
    """HeadsetButton のログ（accepted / why）から ACCEPT / DUP / DROP / NG を決める。"""
    if accepted:
        return "ACCEPT"
    if (why or "").startswith("重複"):
        return "DUP"
    if (why or "").startswith("間引き"):
        return "DROP"
    return "NG"


def build_summary(s: ProbeStats) -> list[str]:
    """診断のまとめ（日本語の行のリスト）。どの経路が生きているかと、次の一手。"""
    if not s.started:
        return ["［開始］を押すと監視を始めます。開始したら、ヘッドセットの多機能ボタンを1回だけ押してください。"]
    out: list[str] = []
    # A
    if not s.a_enabled:
        out.append("HID: 監視していません（" + (s.a_reason or "デバイスが選ばれていません") + "）")
    elif s.a_ok is False:
        out.append(f"HID: 開けませんでした（{s.a_reason}）")
    elif s.a_presses > 0 and s.a_releases > 0:
        out.append("HID: 押す/離すを検出 → 使えます")
    elif s.a_presses > 0:
        out.append("HID: 押すを検出 → 使えます（離すは未検出）")
    elif s.a_reports > 0:
        out.append("HID: レポートは届きましたが、再生/一時停止の押下（01 01）ではありません"
                   " → 選んだデバイスが違うか、別のボタンかもしれません")
    else:
        out.append("HID: 反応なし" + ("" if s.monitoring else "（停止中）"))
    # B
    if not s.b_enabled:
        out.append("ホットキー: 独占しない設定のため登録していません")
    elif s.b_ok is False:
        out.append("ホットキー: 反応なし（登録失敗: " + (s.b_reason or hotkey_error_text(s.b_code)) + "）")
    elif s.b_presses > 0:
        out.append(f"ホットキー: 反応あり（{s.b_presses}回） → 使えます")
    else:
        out.append("ホットキー: 反応なし（登録は成功しています）")
    # C
    if not s.c_enabled:
        out.append("メディアキー: 監視していません")
    elif s.c_ok is False:
        out.append(f"メディアキー: 監視を始められませんでした（{s.c_reason}）")
    elif s.c_total > 0:
        names = "、".join(f"{MEDIA_NAMES.get(vk, hex(vk))} {n}回" for vk, n in sorted(s.c_counts.items()))
        out.append(f"メディアキー: 届いています（{names}）")
    else:
        out.append("メディアキー: 届いていません")
    # コダマ
    if s.k_accept or s.k_dup or s.k_drop:
        out.append(f"コダマの判定: 確定 {s.k_accept} / 重複 {s.k_dup} / 間引き {s.k_drop}")
    # 次の一手
    hints: list[str] = []
    a_alive = s.a_ok is True and s.a_presses > 0
    b_alive = s.b_ok is True and s.b_presses > 0
    if not (a_alive or b_alive or s.c_total):
        hints.append("多機能ボタンを1回だけ押してください（3秒以上あけて1回ずつ。素早い連続押しはヘッドセット内部で処理されて届きません）")
        if s.a_enabled and (s.a_ok is False or (s.a_ok and s.a_reports == 0)):
            hints.append("ドングルが別のアプリ（通話ソフト・ヘッドセット用ユーティリティ等）に占有されていないか確認し、USB を挿し直してみてください")
        if s.b_ok is False and s.b_code == 1409:
            hints.append("ホットキーは、他のアプリが再生/一時停止キーを先に使っているため取れていません")
    else:
        if a_alive and s.b_ok is False:
            hints.append("HID だけで検出できます。ただし、ホットキーを独占できていないので、他のアプリの再生/一時停止も同時に反応することがあります")
        if not a_alive and b_alive:
            hints.append("HID は読めていませんが、ホットキー経由で動きます")
        if not a_alive and not b_alive and s.c_total:
            hints.append("ボタンはキーとして届いていますが、HID・ホットキーでは受け取れていません（登録失敗か、他のアプリが先に処理）")
        if a_alive and b_alive:
            hints.append("両方の経路が生きています。コダマは同じ押下を1回にまとめます（重複は DUP と表示されます）")
        if (a_alive or b_alive) and s.k_accept == 0:
            hints.append("コダマは確定として受け取っていません（重複・間引きのみ）。押す間隔を空けてもう一度試してください")
    if hints:
        out.append("")
        out.extend("次の一手: " + h for h in hints)
    return out
