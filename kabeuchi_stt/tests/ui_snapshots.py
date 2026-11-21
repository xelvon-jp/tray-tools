# -*- coding: utf-8 -*-
"""画面を実際には表示せずに PNG へ保存する（WA_DontShowOnScreen + grab）。前面化しない・入力しない・マイクは開かない。

使い方（PowerShell から。QT_QPA_PLATFORM=offscreen と bash 起動は豆腐になるので使わない）:
  python tests\\ui_snapshots.py <出力フォルダ> [倍率=1.0] [--set 通常|幅|フル]
倍率は QT_SCALE_FACTOR（DPI 100% = 1.0 / 150% = 1.5 / 200% = 2.0）。ファイル名の末尾に _x1.5 のように付く。
本人のデータ置き場は一時フォルダへ向け、履歴などはダミーを入れる。--set 幅 は幅ごとの状態だけ。
"""
import os
import sys
import tempfile
import threading
import time
import types
import wave
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
out_dir = Path(args[0]) if len(args) > 0 else Path(tempfile.mkdtemp())
scale = args[1] if len(args) > 1 else "1.0"
which = "フル"
if "--set" in sys.argv:
    which = sys.argv[sys.argv.index("--set") + 1]
os.environ["QT_SCALE_FACTOR"] = scale
out_dir.mkdir(parents=True, exist_ok=True)
tag = "" if scale == "1.0" else f"_x{scale}"

import _common as C  # noqa: E402,F401  (データ置き場を一時フォルダへ向ける)

import numpy as np  # noqa: E402
from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,  # noqa: E402
                               QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QRadioButton,
                               QSlider, QSpinBox, QVBoxLayout, QWidget)

import ui_style as S  # noqa: E402
from history import History  # noqa: E402
from mic_check import diagnose, measure  # noqa: E402
from playback_devices import OutputDevice  # noqa: E402
from services import build_services  # noqa: E402
from settings import DEFAULTS  # noqa: E402
from test_core_runtime import FakeSD  # noqa: E402
from timeline import Timeline  # noqa: E402
from ui_main import Bridge, MainWindow, SettingsDialog, SuggestDialog  # noqa: E402
from ui_widgets import CheckBox  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="kabeuchi_ui_"))
settings = dict(DEFAULTS)
settings.update(out_dir=str(tmp / "out"), hotwords="予算, 一次集計, API, 年間計画, 品質確認, 自動化, 付箋, 深掘り",
                live_enabled=True)
(tmp / "out").mkdir()

app = QApplication(sys.argv)
app.setFont(S.app_font())
app.setStyleSheet(S.QSS)


def shot(widget, name, size=None, zoom=1):
    widget.setAttribute(Qt.WA_DontShowOnScreen, True)
    if size is not None:
        widget.resize(*size)
    if not widget.isVisible():
        widget.show()
    for _ in range(8):
        app.processEvents()
        time.sleep(0.03)
    p = out_dir / f"{name}{tag}.png"
    pm = widget.grab()
    if zoom != 1:
        pm = pm.scaled(pm.size() * zoom, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.save(str(p))
    print("saved", p.name, f"{widget.width()}x{widget.height()}")


def make_wav(path, sec=20):
    t = np.arange(sec * 16000) / 16000
    x = (np.sin(2 * np.pi * 220 * t) * 3000).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(x.tobytes())


hist = History(tmp / "h.db")
samples = [
    ("2026-11-21T09:12:00", 1815, "small", "今日は壁打ちで、いま考えていることを順番に整理していきたいと思います。予算の確認作業をAIでどこまで自動化できるかを考えています。",
     [(0, 6, "今日は壁打ちで、いま考えていることを順番に整理していきたいと思います。"),
      (7, 20, "予算の確認作業をAIでどこまで自動化できるかを考えています。")]),
    ("2026-11-20T21:40:00", 960, "small", "夕飯のあとに、来週の年間計画の資料について考えた。API連携の優先度をどうするか。", []),
    ("2026-11-19T07:55:00", 420, "base", "朝の散歩中に思いついたこと。付箋アプリの改良案。", []),
]
wav1 = tmp / "out" / "kabeuchi_20261121_091200.wav"
make_wav(wav1)
for i, (when, dur, model, text, segs) in enumerate(samples):
    hist.add(text=text, segments=segs or [(0, 5, text)], duration=dur, model=model, created_at=when,
             wav_path=str(wav1) if i == 0 else "")

gate = threading.Event()


def slow_transcribe(wav, s, model_name=None, on_progress=None, on_status=None, cancel=None, model=None,
                    postprocess=None, **kw):
    on_progress(0.42)
    gate.wait(60)
    from transcriber import FinalResult
    return FinalResult(text="x", segments=[(0, 1, "x")], audio_seconds=10.0, elapsed=1.0)


class Prov:
    def __init__(self, devs):
        self.devs = devs

    def list(self):
        return list(self.devs)


DEVS = [OutputDevice("a", "スピーカー (Realtek Audio)", True), OutputDevice("b", "ヘッドセット (USB Audio)", False),
        OutputDevice("c", "HDMI (NVIDIA)", False)]

# 待機中モニター用の入力（話し声のような音を流し続ける。実マイクは開かない）
sd = FakeSD(C.speechlike(10, -14), speed=10.0, loop=True)
bridge = Bridge()
svc = build_services(settings, on_job_event=lambda j: None, history=hist, transcribe=slow_transcribe,
                     loader=lambda n, t: object(), sd=sd)
prov = Prov(DEVS)
win = MainWindow(svc, bridge, use_tray=False, volume=0.0, muted=True, settings_path=tmp / "settings.json",
                 device_provider=prov)
win.timer.stop()
win.idle_timer.stop()
win._geo_timer.stop()


def make_timeline(final=False, recording=False, rows=5, pause_at=None, silence=False):
    tl = Timeline(30)
    tl.set_recording(recording)
    n_sec = rows * 30 - 12
    rng = np.random.default_rng(1)
    amp = (np.abs(np.sin(np.linspace(0, 60, n_sec * 16000))) * rng.uniform(0.05, 0.6, n_sec * 16000) * 20000).astype("<i2")
    if pause_at is not None:
        k = int(pause_at * 16000)
        tl.feed(amp[:k])
        tl.add_pause(pause_at)
        tl.set_pause_length(0, 83.0)
        tl.feed(amp[k:])
    else:
        tl.feed(amp)
    drafts = [(3, 9, "えーと今日は壁内で今考えていることを整理します"), (12, 24, "予算の確認作業をAIでどこまで自動化できるか"),
              (33, 41, "年間計画では三つの作業のうち二つを自動化"), (47, 56, "一次集計のAI化とAPI連携が今年度の柱"),
              (66, 75, "不線に書き出して頻度で並べる")]
    finals = [(2.5, 9.4, "今日は壁打ちで、いま考えていることを整理します。"),
              (11.8, 24.0, "予算の確認作業を、AIでどこまで自動化できるかを考えています。"),
              (32.6, 41.2, "いまの年間計画では、三つの作業のうち二つを自動化する、という目標があります。"),
              (46.5, 56.0, "今年度は、一次集計のAI化とAPI連携が柱になります。"),
              (65.8, 75.0, "付箋に書き出して、頻度で並べて、深掘りする順番を決めたいです。")]
    if silence:
        # 発話は最初の行だけ。あとは無音（畳まれて「無音 2:30」になる）
        tl.set_final(finals[:1], n_sec)
    elif final:
        tl.set_final(finals, n_sec)
    else:
        for a, b, t in drafts[:rows]:
            tl.add_draft(a, b, t)
        if recording:
            tl._drafts = tl._drafts[:-1]
    return tl


def show_tl(tl):
    win.timeline_view.set_timeline(tl)
    win.timeline_view.refresh()


def set_mode(compact):
    if win._compact != compact:
        win.set_compact(compact)


def reset_normal(width=1320, height=820):
    set_mode(False)
    win.idle_band.setVisible(False)
    win.resize(width, height)


def run_ticks(sec):
    """待機中モニターを実際に動かして（偽の入力）、tick を手で回す。タイマーは止めてあるので手動。"""
    end = time.time() + sec
    while time.time() < end:
        win._tick()
        app.processEvents()
        time.sleep(0.05)


# ===================================================================== 幅ごとの状態（要望: 480/560/700/1000/1400）
def width_set():
    tl = make_timeline(recording=True, rows=4, pause_at=40)
    show_tl(tl)
    win._set_recording_ui(True)
    win.lbl_time.setText("00:03:28")
    win.meter.set_level(0.62)
    win._set_issues([], recording=True)
    win.lbl_live_state.setText("逐次認識: 処理済み 4・待ち 0")
    for w in (480, 560, 700, 1000, 1400):
        reset_normal(w, 760)
        shot(win, f"10_width_{w}_recording")
    # 同じ幅で、マイクの指摘が長いとき（バッジが省略される・メーターが潰れない）
    x = np.concatenate([C.noise(4, -42, 1), C.sine(3, -40)])
    iss = diagnose(measure(x), expect_speech=False)
    win._set_issues(iss, recording=True)
    for w in (480, 700, 1400):
        reset_normal(w, 760)
        shot(win, f"11_width_{w}_mic_warning")
    # 待機中（録音していない）
    win._set_recording_ui(False)
    win._set_issues([], recording=False)
    win.lbl_live_state.setText("")
    win._mic_badge_text, win._mic_badge_style = "● マイクOK", "background: #1f3a2c; color: #46c27a; border-radius: 15px; padding: 3px 14px;"
    win._set_issues([], recording=False)
    for w in (480, 560, 700, 1000, 1400):
        reset_normal(w, 700)
        shot(win, f"12_width_{w}_idle")
    # コンパクトの幅
    set_mode(True)
    win._set_recording_ui(True)
    win._set_issues([], recording=True)
    for w in (380, 420, 480, 560, 700):
        win.resize(w, 300)
        shot(win, f"13_compact_width_{w}")
    win._set_recording_ui(False)
    set_mode(False)


if which == "幅":
    width_set()
    print("done")
    sys.exit(0)

# ===================================================================== 1. 録音中（波形→仮認識）
reset_normal()
tl = make_timeline(recording=True, rows=5)
show_tl(tl)
win._set_recording_ui(True)
win.lbl_time.setText("00:02:28")
win.meter.set_level(0.62)
win._set_issues([], recording=True)
win.lbl_live_state.setText("逐次認識: 処理済み 4・待ち 0")
shot(win, "01_recording")

# ===================================================================== 2. マイクの指摘
import _common as CM  # noqa: E402
x = np.concatenate([CM.noise(4, -42, 1), CM.sine(3, -40)])
iss = diagnose(measure(x), expect_speech=False)
win.meter.set_level(0.18)
win._set_issues(iss, recording=True)
shot(win, "02_mic_warning")
win.meter.set_level(0.0)
win._set_issues(diagnose(measure(np.zeros(48000, dtype=np.int16))), recording=True)
shot(win, "02b_mic_silent")

# ===================================================================== 3. 一時停止中（印の行・再開ボタン）
tlp = make_timeline(recording=True, rows=4, pause_at=40)
show_tl(tlp)
win._set_recording_ui(True)
win.svc.session.rec = types.SimpleNamespace(is_recording=True, is_paused=True)   # 画像用の見せかけ（録音はしていない）
win._set_paused_ui(True)
win.lbl_time.setText("00:00:40")
win.meter.set_level(0.35)
win._set_issues([], recording=True)
win.lbl_live_state.setText("一時停止中 1:23（録音は止まっています）")
shot(win, "03_paused")
win.svc.session.rec = None
win._set_paused_ui(False)

# ===================================================================== 4. 停止後・清書中
win._set_recording_ui(False)
win._set_issues([], recording=False)
win.lbl_live_state.setText("")
win.lbl_time.setText("00:02:28")
win.meter.set_level(0.0)
tl.set_recording(False)
show_tl(tl)
win.svc.queue.submit(wav1, timeline=tl)
time.sleep(0.4)
win.svc.queue.submit(tmp / "out" / "second.wav")
win._update_queue_ui()
shot(win, "04_finalizing")

# ===================================================================== 5. 清書完了
show_tl(make_timeline(final=True))
stats = measure(CM.speechlike(3, -12))
win.show_mic_test_result(stats, [])
win.lbl_time.setText("00:00:00")
shot(win, "05_final_done")
win.svc.queue.close()
gate.set()

# ===================================================================== 6. 無音の畳み込み（発話なしの連続が 1 行に）
win._mic_test_info = ""
win._mic_test_until = 0
tls = make_timeline(silence=True, rows=6)
show_tl(tls)
win._set_issues([], recording=False)
shot(win, "06_silence_collapsed")

# ===================================================================== 7. 待機中モニター（録音していないのにメーターが動く）＋「録音していません」の帯
show_tl(make_timeline(final=True))
win.svc.session.apply_monitor_now(True)
win.s["idle_prompt_speech_sec"] = 1.0
run_ticks(2.5)
shot(win, "07_idle_monitor_meter")
win.show_idle_band()
shot(win, "08_idle_band_not_recording")
win.hide_idle_band()

# ===================================================================== 9. コンパクト（通常 / 録音中 / 一時停止 / 帯 / 清書中）
set_mode(True)
win.resize(420, 300)
show_tl(make_timeline(recording=True, rows=4))
win._set_recording_ui(True)
win.lbl_time.setText("00:02:28")
win.meter.set_level(0.55)
win._set_issues([], recording=True)
shot(win, "09_compact_recording")
win.svc.session.rec = types.SimpleNamespace(is_recording=True, is_paused=True)
win._set_paused_ui(True)
show_tl(make_timeline(recording=True, rows=4, pause_at=40))
shot(win, "09b_compact_paused")
win.svc.session.rec = None
win._set_paused_ui(False)
win._set_recording_ui(False)
win.show_idle_band()
shot(win, "09c_compact_idle_band")
win.hide_idle_band()
win.lbl_queue_compact.setText("清書中 42%　待機 1 件")
shot(win, "09d_compact_finalizing")
set_mode(False)
win.svc.session.apply_monitor_now(False)

# ===================================================================== 10. 辞書編集・右パネル
win._mic_test_info = ""
win._set_issues([], recording=False)
win.chips.add_word("深掘り")
win.chips.add_word("ベースバンド")
win.tbl.blockSignals(True)
win.add_dict_pairs([("壁内", "壁打ち"), ("頻度", "頻度")])
win.tbl.blockSignals(False)
win.tbl.setCurrentCell(2, 0)
win.tbl.editItem(win.tbl.item(2, 0))
shot(win, "14_dict_edit")

# ===================================================================== 11. 履歴タブ（再生出力先のコンボ）
win.tabs.setCurrentIndex(1)
win.reload_history()
win.lst_hist.setCurrentRow(0)
win.open_record(1)
win.editor.highlight_block(1)
shot(win, "15_history_playback_device")
win.cmb_out.setCurrentIndex(3)                    # HDMI を選ぶ
shot(win, "15b_history_device_selected")
prov.devs = [DEVS[0], DEVS[1]]                   # HDMI が外れた → システム既定へ戻して知らせる
win.player._on_devices_changed()
shot(win, "15c_history_device_vanished")
win.reload_history()
win.ed_search.setText("予算")
win.reload_history()
shot(win, "16_history_search")
win.ed_search.setText("")
win.tabs.setCurrentIndex(0)
reset_normal()

# ===================================================================== 12. ダイアログ
dlg = SuggestDialog([("不線", "付箋"), ("壁内", "壁打ち"), ("一時集計", "一次集計")])
dlg._checks[1][0].setChecked(False)
shot(dlg, "17_suggest_dialog")
sd_ = SettingsDialog(settings, [{"index": 1, "name": "マイク配列 (Realtek Audio)", "hostapi": "MME"}])
shot(sd_, "18_settings_dialog")
sd_.idle_monitor.setChecked(False)
sd_.para_on.setChecked(False)
shot(sd_, "18b_settings_dialog_off")

# ===================================================================== 13. チェックボックス（ON/OFF の拡大）と入力部品の一覧
panel = QFrame()
panel.setStyleSheet(f"QFrame {{ background: {S.PANEL}; }}")
g = QGridLayout(panel)
g.setContentsMargins(18, 14, 18, 14)
g.setHorizontalSpacing(30)
g.setVerticalSpacing(12)
heads = ["通常", "ホバー", "フォーカス", "無効"]
for c, h in enumerate(heads):
    lab = QLabel(h)
    lab.setStyleSheet(f"color: {S.MUTED}; background: transparent;")
    g.addWidget(lab, 0, c + 1)
for r, (name_, on) in enumerate((("ON", True), ("OFF", False)), start=1):
    lab = QLabel(name_)
    lab.setStyleSheet(f"color: {S.MUTED}; background: transparent; font-weight: bold;")
    g.addWidget(lab, r, 0)
    for c in range(4):
        cb = CheckBox("逐次認識")
        cb.setChecked(on)
        if c == 1:
            cb.underMouse = lambda: True
        if c == 2:
            cb.hasFocus = lambda: True
        if c == 3:
            cb.setEnabled(False)
        g.addWidget(cb, r, c + 1)
shot(panel, "19_checkbox_on_off_zoom", zoom=3)

gal = QFrame()
gal.setStyleSheet(f"QFrame#gal {{ background: {S.BG}; }}")
gal.setObjectName("gal")
v = QHBoxLayout(gal)
v.setContentsMargins(20, 16, 20, 16)
v.setSpacing(30)
for dis in (False, True):
    col = QFormLayout()
    col.setVerticalSpacing(10)
    t = QLabel("無効" if dis else "有効")
    t.setStyleSheet(f"color: {S.MUTED}; background: transparent; font-weight: bold;")
    col.addRow(t)
    le = QLineEdit("予算の評価")
    le.setEnabled(not dis)
    cb = QComboBox()
    cb.addItems(["small", "medium", "large-v3"])
    cb.setEnabled(not dis)
    sp = QSpinBox()
    sp.setRange(0, 7200)
    sp.setValue(180)
    sp.setSuffix(" 秒（0=無効）")
    sp.setEnabled(not dis)
    dsp = QDoubleSpinBox()
    dsp.setValue(2.0)
    dsp.setSuffix(" 秒以上")
    dsp.setEnabled(not dis)
    r1, r2 = QRadioButton("選択肢 A（ON）"), QRadioButton("選択肢 B（OFF）")
    r1.setChecked(True)
    r1.setEnabled(not dis)
    r2.setEnabled(not dis)
    c1, c2 = CheckBox("逐次認識（ON）"), CheckBox("クリップボードへコピー（OFF）")
    c1.setChecked(True)
    c1.setEnabled(not dis)
    c2.setEnabled(not dis)
    sl = QSlider(Qt.Horizontal)
    sl.setRange(0, 100)
    sl.setValue(40)
    sl.setEnabled(not dis)
    pb = QPushButton("通常ボタン")
    pb.setEnabled(not dis)
    pr = QPushButton("主ボタン")
    pr.setObjectName("primary")
    pr.setEnabled(not dis)
    tg = QPushButton("📌 最前面")
    tg.setCheckable(True)
    tg.setChecked(True)
    tg.setEnabled(not dis)
    bar = QProgressBar()
    bar.setValue(42)
    bar.setEnabled(not dis)
    for lab, w_ in (("テキスト", le), ("コンボ", cb), ("スピン", sp), ("小数", dsp), ("ラジオ", r1), ("", r2),
                    ("チェック", c1), ("", c2), ("スライダ", sl), ("ボタン", pb), ("", pr), ("トグル(ON)", tg), ("進捗", bar)):
        l_ = QLabel(lab)
        l_.setStyleSheet(f"color: {S.MUTED}; background: transparent;")
        col.addRow(l_, w_)
    v.addLayout(col)
shot(gal, "20_inputs_gallery")

# ===================================================================== 15. フェーズ1.6（ボタン2案・表示サイズ・未録音の赤メーター・帯・右クリックメニュー）
def fake_state(recording=False, paused=False):
    win.svc.session.rec = types.SimpleNamespace(is_recording=True, is_paused=paused) if recording else None
    win._set_recording_ui(recording)
    win._set_paused_ui(recording and paused)


def v16_set():
    reset_normal()
    win._mic_test_info = ""
    win._mic_test_until = 0
    # --- 通常: 録音中 / 待機中 / 一時停止中の帯
    show_tl(make_timeline(recording=True, rows=4, pause_at=40))
    fake_state(True)
    win.lbl_time.setText("00:03:28")
    win.meter.set_level(0.6)
    win._set_issues([], recording=True)
    win.lbl_live_state.setText("逐次認識: 処理済み 4・待ち 0")
    shot(win, "v16_01_normal_recording")
    fake_state(True, paused=True)
    win.lbl_live_state.setText("一時停止中 0:42（録音は止まっています）")
    win.meter.set_level(0.55)
    win.meter.set_unrecorded(True)
    win.show_idle_band("paused")
    shot(win, "v16_02_normal_paused_band_red_meter")
    win.hide_idle_band()
    win.meter.set_unrecorded(False)
    fake_state(False)
    win.lbl_live_state.setText("")
    show_tl(make_timeline(final=True))
    win._set_issues([], recording=False)
    win.meter.set_level(0.5)
    win.meter.set_unrecorded(True)
    win.show_idle_band("idle")
    shot(win, "v16_03_normal_idle_band_red_meter")
    win.hide_idle_band()
    win.meter.set_unrecorded(False)

    # --- コンパクト: 録音中 / 一時停止の帯 / 録音していない帯（赤メーター）
    set_mode(True)
    win.resize(420, 300)
    show_tl(make_timeline(recording=True, rows=4))
    fake_state(True)
    win.lbl_time.setText("00:02:28")
    win.meter.set_level(0.55)
    win._set_issues([], recording=True)
    shot(win, "v16_04_compact_recording")
    fake_state(True, paused=True)
    win.meter.set_unrecorded(True)
    win.show_idle_band("paused")
    shot(win, "v16_05_compact_paused_band_red_meter")
    win.hide_idle_band()
    fake_state(False)
    win.show_idle_band("idle")
    shot(win, "v16_06_compact_idle_band_red_meter")
    win.hide_idle_band()
    for w_ in (380, 420, 480, 560, 700):
        win.resize(w_, 300)
        win.meter.set_unrecorded(True)
        fake_state(False)
        shot(win, f"v16_07_compact_red_meter_{w_}")
    win.meter.set_unrecorded(False)
    set_mode(False)

    # --- ボタン 2 案（通常幅と狭い幅）
    for mode_, name_ in (("label", "case1_icon_label"), ("icon", "case2_icon_only")):
        win.apply_button_style(mode_)
        fake_state(False)
        win.lbl_time.setText("00:00:00")
        win.meter.set_unrecorded(False)
        win.btn_pin.setChecked(True)
        win._set_issues([], recording=False)
        for w_ in (480, 700, 1000, 1400):
            reset_normal(w_, 520)
            shot(win, f"v16_10_buttons_{name_}_{w_}")
        win.btn_pin.setChecked(False)
        fake_state(True)
        win.lbl_time.setText("00:01:12")
        for w_ in (480, 1000):
            reset_normal(w_, 520)
            shot(win, f"v16_11_buttons_{name_}_recording_{w_}")
        fake_state(False)
        set_mode(True)
        win.resize(420, 300)
        shot(win, f"v16_12_buttons_{name_}_compact")
        set_mode(False)
    win.apply_button_style("label")

    # --- 表示サイズ 小 / 標準 / 大
    for size_ in ("small", "standard", "large"):
        win.apply_display_size(size_)
        show_tl(make_timeline(recording=True, rows=4))
        fake_state(True)
        win.lbl_time.setText("00:02:28")
        win.meter.set_level(0.6)
        win._set_issues([], recording=True)
        reset_normal(1100, 700)
        shot(win, f"v16_20_size_{size_}_normal")
        reset_normal(480, 700)
        shot(win, f"v16_21_size_{size_}_480")
        set_mode(True)
        win.resize(420, 300)
        shot(win, f"v16_22_size_{size_}_compact")
        set_mode(False)
        fake_state(False)
        dlg_ = SettingsDialog(dict(settings, display_size=size_), [])
        shot(dlg_, f"v16_23_size_{size_}_settings")
    win.apply_display_size("small")
    reset_normal()

    # --- 右クリックメニュー（実際には popup しない。QMenu をそのまま画像化）
    def cap(menu, pos):
        cap.n += 1
        shot(menu, f"v16_30_context_menu_{cap.n}")
        return None
    cap.n = 0
    win._menu_exec = cap
    tl_ = make_timeline(final=True)
    show_tl(tl_)
    win._on_timeline_context(1, None)
    set_mode(True)
    win.resize(420, 300)
    shot(win, "v16_31_compact_timeline_for_menu")
    win._on_timeline_context(1, None)
    set_mode(False)
    win.tabs.setCurrentIndex(1)
    win.reload_history()
    win.lst_hist.setCurrentRow(0)
    win.open_record(1)
    win._on_history_context(1, "予算の確認作業", None)
    win.tabs.setCurrentIndex(0)
    reset_normal()


v16_set()

# ===================================================================== 14. 幅ごとの状態
width_set()
win.svc.session.close()
print("done")
