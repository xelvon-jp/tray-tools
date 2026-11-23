# -*- coding: utf-8 -*-
"""統合 GUI（S3 3行窓＋F3 引き出し）を、画面に出さずに PNG へ保存する（WA_DontShowOnScreen + grab）。
前面化しない・入力しない・マイクは開かない（FakeSD）・モデルは読まない。ヘッドセット・宛先ワーカーも起動しない。

使い方（PowerShell から。QT_QPA_PLATFORM=offscreen と bash 起動は日本語が豆腐になるので使わない）:
  python tests\\ui_snapshots_unified.py <出力フォルダ> [倍率=1.0]
"""
import os
import sys
import tempfile
import time
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
out_dir = Path(args[0]) if args else Path(tempfile.mkdtemp())
scale = args[1] if len(args) > 1 else "1.0"
os.environ["QT_SCALE_FACTOR"] = scale
out_dir.mkdir(parents=True, exist_ok=True)
tag = "" if scale == "1.0" else f"_x{scale}"

import _common as C  # noqa: E402,F401  (データ置き場を一時フォルダへ向ける)

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import ui_style as S  # noqa: E402
import ui_unified_logic as U  # noqa: E402
from history import History  # noqa: E402
from services import build_services  # noqa: E402
from settings import DEFAULTS  # noqa: E402
from test_core_runtime import FakeSD  # noqa: E402
from timeline import Timeline  # noqa: E402
from ui_main import Bridge, MainWindow  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
display = os.environ.get("KABEUCHI_SNAP_SIZE", "standard")
S.set_size(display)
app.setFont(S.app_font())
app.setStyleSheet(S.QSS)
tmp = Path(tempfile.mkdtemp(prefix="kabeuchi_ui_u_"))
(tmp / "out").mkdir()


def pump(n=8):
    for _ in range(n):
        app.processEvents()
        time.sleep(0.03)


def make_window(dialogue=True, drawer=False):
    s = dict(DEFAULTS)
    s["dialogue"] = dict(DEFAULTS["dialogue"])
    s["dialogue"].update(enabled=dialogue)
    s.update(out_dir=str(tmp / "out"), live_enabled=False, silence_stop_sec=0, display_size=display,
             hotwords="基地局, 一次解析, RAG", unified_drawer_open=drawer, idle_monitor=False)
    hist = History(tmp / f"h{time.time_ns()}.db")
    for i, (when, dur, text) in enumerate([
            ("2026-11-23T09:12:00", 728, "今日の打ち合わせの論点を、三つに絞って整理します。"),
            ("2026-11-22T21:40:00", 235, "週末の作業の段取りを、先に決めておきたいです。"),
            ("2026-11-22T13:05:00", 1531, "レビューの観点を、チェックリストにまとめ直します。")]):
        hist.add(text=text, segments=[(0, 5, text)], duration=dur, model="small", created_at=when, wav_path="")
    bridge = Bridge()
    svc = build_services(s, on_job_event=lambda j: bridge.job_event.emit(j), on_status=lambda m: bridge.status.emit(m),
                         sd=FakeSD(C.speechlike(8, -12), speed=10.0, loop=True),
                         transcribe=lambda *a, **k: None, loader=lambda n, t: object(), history=hist)
    w = MainWindow(svc, bridge, use_tray=False, volume=0.0, muted=True, settings_path=tmp / f"s{time.time_ns()}.json")
    w._warn = lambda t, m: None
    w.timer.stop()
    w.idle_timer.stop()
    w._geo_timer.stop()
    if w.dialogue_panel is not None:
        w.dialogue_panel.timer.stop()
        w.dialogue_panel._confirm = lambda t, m: True
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    pump()
    return w, svc, hist


def shot(w, name, size=None):
    if size is not None:
        w.resize(*size)
    pump()
    if w.dialogue_panel is not None:
        w.dialogue_panel.refresh()
    w.unified.refresh()
    pump(4)
    p = out_dir / f"{name}{tag}.png"
    w.grab().save(str(p))
    print("saved", p.name, f"{w.width()}x{w.height()}")


def close(w, svc, hist):
    w.timer.stop()
    try:
        w._shutdown_dialogue()
    except Exception:
        pass
    svc.session.close()
    svc.queue.close()
    hist.close()
    w.close()


def feed_dialogue_text(w):
    """対話モード: 確定済み（白）・認識中（波形）を作る。"""
    ds = w.dialogue_ctl.ds
    ds.buffer.add_utterance("二つ目は、担当の分け方です。", 20.0, 24.0)
    ds.buffer.add_utterance("ここは少し迷っていて、案を二つ出して比べたいと思います。", 25.0, 31.0)
    ds.pending_waves = lambda: [{"uid": 50.0, "env": (1, 3, 5, 7, 4, 2, 6, 8, 3, 1, 4, 6, 7, 3, 2, 5, 3, 1)}]


def start_dialogue(w):
    w.dialogue_ctl._recognizer = lambda audio, ctx="": ""
    w.dialogue_panel.start_session()
    pump(4)
    feed_dialogue_text(w)


# ---------------------------------------------------------------- 1. S3 既定（AI対話・停止中／セッション中）
w, svc, hist = make_window()
shot(w, "u01_s3_dialogue_idle")
start_dialogue(w)
shot(w, "u02_s3_dialogue_recording")
w.dialogue_panel.set_expanded(True)
shot(w, "u03_s3_expanded")
w.dialogue_panel.set_expanded(False)

# 未配達の行と通知・マイク詳細の吹き出し
buf = w.dialogue_ctl.ds.buffer
buf.add_utterance("これは届けられなかった内容です。", 60.0, 64.0)
req = buf.commit()
if req is not None:
    buf.report(req, {"status": "undelivered", "reason": "target_not_found"})
w.dialogue_panel.set_notice("届けられませんでした: 宛先の窓が見つかりません（内容は保持しています）", "warn")
shot(w, "u04_s3_undelivered")
w.unified.chip.set_state("low")
w.unified.popup.show_for(w.unified.chip, "low", [])
shot(w, "u05_s3_chip_popup")
w.unified.popup.hide()
close(w, svc, hist)

# ---------------------------------------------------------------- 2. マイク状態チップ 全状態（大きさは同じ）
w, svc, hist = make_window()
imgs = []
for key in U.CHIP_ORDER:
    w.unified.chip.set_state(key)
    pump(2)
    imgs.append(w.unified.row_top.grab().toImage())
wd = max(i.width() for i in imgs)
ht = sum(i.height() for i in imgs)
strip = QImage(wd, ht, QImage.Format_ARGB32)
strip.fill(Qt.black)
pa = QPainter(strip)
y = 0
for i in imgs:
    pa.drawImage(0, y, i)
    y += i.height()
pa.end()
strip.save(str(out_dir / f"u06_chip_states{tag}.png"))
print("saved u06_chip_states", strip.width(), strip.height(), "chip", w.unified.chip.width(), w.unified.chip.height())
close(w, svc, hist)

# ---------------------------------------------------------------- 3. F3 引き出し（履歴・用語・Copilot・設定）
w, svc, hist = make_window(drawer=True)
w.reload_history()
w.lst_hist.setCurrentRow(0)
pump()
start_dialogue(w)
w.unified.drawer.set_tab(0)
shot(w, "u07_f3_drawer_history", size=(860, 700))
w.unified.drawer.set_tab(1)
shot(w, "u08_f3_drawer_terms")
w.unified.drawer.set_tab(2)
shot(w, "u09_f3_drawer_copilot")
w.unified.drawer.set_tab(3)
shot(w, "u10_f3_drawer_settings")
close(w, svc, hist)

# ---------------------------------------------------------------- 5. 宛先「なし（記録だけ）」と Copilot の前置き
w, svc, hist = make_window()
w.dialogue_ctl.set_record_only(True)
w.dialogue_panel.fill_presets()
shot(w, "u13_s3_dest_none")
close(w, svc, hist)
print("done", out_dir)
