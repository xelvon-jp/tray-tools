# -*- coding: utf-8 -*-
"""壁打ち文字起こし GUI（tkinter）と CLI の入口。

  GUI           : python app.py
  清書のやり直し : python app.py --wav out\\xxx.wav [--model medium]
  デバイス一覧   : python app.py --list-devices
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import queue
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PYTHONUTF8", "1")

from settings import load_settings, resolve_dir  # noqa: E402
from version import VERSION  # noqa: E402


def _enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class KabeuchiApp:
    def __init__(self, settings: dict, auto_close_sec: float | None = None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        from recorder import Recorder
        from transcriber import LiveTranscriber
        self.Recorder, self.LiveTranscriber = Recorder, LiveTranscriber

        self.s = settings
        self.ui_q: queue.Queue = queue.Queue()
        self.live_lines: list[str] = []
        self.live: LiveTranscriber | None = None
        self.rec = None
        self.busy_final = False
        self.cancel = threading.Event()

        root = self.root = tk.Tk()
        root.title(f"壁打ち文字起こし  v{VERSION}")
        # DPI 100〜200% で読める大きさ: tk のスケーリングに合わせてフォントを決める
        scale = root.winfo_fpixels("1i") / 96.0
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
        base = 12
        self.f_norm = ("Yu Gothic UI", base)
        self.f_big = ("Yu Gothic UI", base + 6, "bold")
        self.f_text = ("Yu Gothic UI", base + 2)
        root.geometry(f"{int(900 * scale)}x{int(720 * scale)}")
        root.minsize(int(720 * scale), int(560 * scale))
        ttk.Style().configure("TButton", font=self.f_norm, padding=6)

        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        self.btn = ttk.Button(top, text="● 録音開始", command=self.toggle)
        self.btn.pack(side="left")
        self.lbl_time = tk.Label(top, text="00:00:00", font=self.f_big, width=9)
        self.lbl_time.pack(side="left", padx=14)
        self.meter = tk.Canvas(top, width=260, height=28, bg="#222", highlightthickness=1)
        self.meter.pack(side="left", padx=6)
        self.meter_bar = self.meter.create_rectangle(0, 0, 0, 28, fill="#3c3", width=0)
        self.lbl_warn = tk.Label(top, text="", fg="#c00", font=self.f_norm)
        self.lbl_warn.pack(side="left", padx=8)

        row2 = ttk.Frame(root, padding=(10, 0))
        row2.pack(fill="x")
        ttk.Button(row2, text="WAVから清書…", command=self.pick_wav).pack(side="left")
        ttk.Button(row2, text="出力フォルダを開く", command=self.open_out).pack(side="left", padx=8)
        self.live_var = tk.BooleanVar(value=bool(self.s.get("live_enabled", True)))
        ttk.Checkbutton(row2, text="逐次認識（録音中の仮表示）", variable=self.live_var).pack(side="left", padx=8)

        tk.Label(root, text="録音中の仮認識（直近の数行）", font=self.f_norm, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        self.txt_live = tk.Text(root, height=max(2, int(self.s.get("live_lines", 4))) + 1,
                                font=self.f_text, wrap="word", state="disabled", bg="#f4f4f4")
        self.txt_live.pack(fill="x", padx=10)

        tk.Label(root, text="清書結果", font=self.f_norm, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        fr = ttk.Frame(root)
        fr.pack(fill="both", expand=True, padx=10)
        self.txt_final = tk.Text(fr, font=self.f_text, wrap="word")
        sb = ttk.Scrollbar(fr, command=self.txt_final.yview)
        self.txt_final.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt_final.pack(side="left", fill="both", expand=True)

        self.prog = ttk.Progressbar(root, maximum=100)
        self.prog.pack(fill="x", padx=10, pady=(8, 0))
        self.lbl_status = tk.Label(root, text="待機中", anchor="w", font=self.f_norm, justify="left", wraplength=int(860 * scale))
        self.lbl_status.pack(fill="x", padx=10, pady=6)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.tick)
        if auto_close_sec:
            root.after(int(auto_close_sec * 1000), root.destroy)

    # ---------- 操作
    def toggle(self):
        if self.rec is not None and self.rec.is_recording:
            self.stop_and_finalize()
        else:
            self.start_recording()

    def start_recording(self):
        if self.busy_final:
            self.status("清書中は録音を始められません")
            return
        out_dir = resolve_dir(self.s.get("out_dir", "out"))
        wav = out_dir / f"kabeuchi_{dt.datetime.now():%Y%m%d_%H%M%S}.wav"
        self.live = None
        on_audio = None
        if self.live_var.get():
            self.live = self.LiveTranscriber(self.s, on_text=lambda t: self.ui_q.put(("live", t)),
                                             on_status=lambda m: self.ui_q.put(("status", m)))
            on_audio = self.live.feed
        self.rec = self.Recorder(device=self.s.get("input_device"), on_audio=on_audio,
                                 on_error=lambda m: self.ui_q.put(("status", m)))
        try:
            self.rec.start(wav)
        except Exception as e:
            self.status(f"録音を開始できません: {e}")
            self.rec = None
            return
        if self.live:
            self.live.start()
        self.live_lines.clear()
        self.set_live_text()
        self.btn.configure(text="■ 停止して清書")
        self.status(f"録音中: {wav}")

    def stop_and_finalize(self):
        wav = self.rec.stop()
        if self.live:
            self.live.stop(wait=2)
            self.live = None
        self.btn.configure(text="● 録音開始")
        self.status(f"録音を保存しました: {wav}")
        self.run_final(Path(wav))

    def pick_wav(self):
        from tkinter import filedialog
        if self.busy_final or (self.rec and self.rec.is_recording):
            return
        p = filedialog.askopenfilename(initialdir=str(resolve_dir(self.s.get("out_dir", "out"))),
                                       filetypes=[("WAV", "*.wav"), ("すべて", "*.*")])
        if p:
            self.run_final(Path(p))

    def run_final(self, wav: Path):
        from transcriber import transcribe_wav
        self.busy_final = True
        self.prog["value"] = 0
        self.txt_final.delete("1.0", "end")
        q = self.ui_q

        def work():
            try:
                r = transcribe_wav(wav, self.s,
                                   on_progress=lambda p: q.put(("prog", p)),
                                   on_status=lambda m: q.put(("status", m)),
                                   cancel=self.cancel)
                q.put(("final", r))
            except Exception as e:
                q.put(("final_err", str(e)))
        threading.Thread(target=work, name="kabeuchi-final", daemon=True).start()

    def open_out(self):
        d = resolve_dir(self.s.get("out_dir", "out"))
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))  # type: ignore[attr-defined]
        except Exception as e:
            self.status(f"開けません: {e}")

    def on_close(self):
        from tkinter import messagebox
        if self.rec is not None and self.rec.is_recording:
            if not messagebox.askokcancel("録音中", "録音を止めて終了します（WAV は残ります。清書は後で「WAVから清書」で）。"):
                return
            self.rec.stop()
        if self.busy_final:
            self.cancel.set()
        self.root.destroy()

    # ---------- 表示
    def status(self, m: str):
        self.lbl_status.configure(text=m)

    def set_live_text(self):
        n = max(1, int(self.s.get("live_lines", 4)))
        self.txt_live.configure(state="normal")
        self.txt_live.delete("1.0", "end")
        self.txt_live.insert("end", "\n".join(self.live_lines[-n:]))
        self.txt_live.configure(state="disabled")

    def tick(self):
        try:
            while True:
                kind, v = self.ui_q.get_nowait()
                if kind == "live":
                    self.live_lines.append(v)
                    self.live_lines = self.live_lines[-50:]
                    self.set_live_text()
                elif kind == "status":
                    self.status(v)
                elif kind == "prog":
                    self.prog["value"] = v * 100
                    self.status(f"清書中… {v * 100:.0f}%")
                elif kind == "final":
                    self.finish_final(v)
                elif kind == "final_err":
                    self.busy_final = False
                    self.status("清書できませんでした。詳細は下の欄")
                    self.txt_final.delete("1.0", "end")
                    self.txt_final.insert("1.0", v)
        except queue.Empty:
            pass
        rec = self.rec
        if rec is not None and rec.is_recording:
            e = int(rec.elapsed)
            self.lbl_time.configure(text=f"{e // 3600:02d}:{e % 3600 // 60:02d}:{e % 60:02d}")
            lv = rec.level
            self.meter.coords(self.meter_bar, 0, 0, 260 * lv, 28)
            stalled = rec.seconds_since_audio > 2.0
            self.lbl_warn.configure(text="⚠ 音が届いていません" if stalled else "")
        else:
            self.meter.coords(self.meter_bar, 0, 0, 0, 28)
        self.root.after(100, self.tick)

    def finish_final(self, r):
        self.busy_final = False
        self.txt_final.insert("1.0", r.text)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(r.text)
            self.root.update()
            clip = "クリップボードにコピー済み"
        except Exception as e:
            clip = f"クリップボード失敗: {e}"
        self.status(f"完了（{r.elapsed:.0f}秒 / 音声{r.audio_seconds:.0f}秒）  {r.txt_path}  {r.md_path}  {clip}")

    def run(self):
        self.root.mainloop()


def cli_final(wav: str, model: str | None, settings: dict) -> int:
    from transcriber import transcribe_wav
    last = [-1]

    def prog(p):
        pct = int(p * 100)
        if pct // 10 != last[0]:
            last[0] = pct // 10
            print(f"  進捗 {pct}%", flush=True)
    r = transcribe_wav(Path(wav), settings, model_name=model, on_progress=prog)
    print(r.text)
    print(f"\n保存: {r.txt_path}\n保存: {r.md_path}\n所要 {r.elapsed:.1f}秒 / 音声 {r.audio_seconds:.1f}秒")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"壁打ち文字起こし v{VERSION}")
    ap.add_argument("--wav", help="既存の WAV を清書だけやり直す（GUIなし）")
    ap.add_argument("--model", help="清書モデルを一時的に変更（small / medium / large-v3）")
    ap.add_argument("--list-devices", action="store_true", help="入力デバイス一覧（マイクは開かない）")
    ap.add_argument("--auto-close", type=float, help="GUI を指定秒数で自動終了（表示確認用）")
    a = ap.parse_args(argv)
    settings = load_settings()
    if a.list_devices:
        from recorder import list_input_devices
        for d in list_input_devices():
            print(f"{d['index']:3d}  {'*' if d['is_default'] else ' '} {d['name']}  [{d['hostapi']}]")
        return 0
    if a.wav:
        from transcriber import ModelUnavailableError
        try:
            return cli_final(a.wav, a.model, settings)
        except ModelUnavailableError as e:
            print(e)
            return 2
    _enable_dpi_awareness()
    KabeuchiApp(settings, a.auto_close).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
