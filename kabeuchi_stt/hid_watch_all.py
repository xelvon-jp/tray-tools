# -*- coding: utf-8 -*-
"""ヘッドセット（3511:2EF2）の HID インターフェースを、全部同時に読んで、届いたレポートを出す診断用（読み取り専用）。
コダマ本体でセッション中（マイクを使用中）に、ボタンが別のインターフェース（電話用など）に出ていないかを調べる。
使い方: コダマでセッションを始める → このツールを起動 → 多機能ボタンを3秒あけて数回押す → 90秒で自動終了（Ctrl+C でも終了）。"""
import sys, time, threading, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dialogue_hid as D

VID_PID = "3511:2EF2"
SECONDS = 90
LOG = Path(__file__).resolve().parent / "stage0" / "probe_logs" / ("hid_all_%s.txt" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
lock = threading.Lock()
stop = threading.Event()
counts = {}


def out(msg):
    line = "%s %s" % (datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3], msg)
    with lock:
        print(line, flush=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def watch(info):
    name = "%s Page0x%X/Usage0x%X %dB" % (D.short_path(info.path), info.usage_page, info.usage, info.input_len)
    try:
        src = D.open_source(info)
    except Exception as e:
        out("[%s] 開けません: %s" % (name, e))
        return
    out("[%s] 読み取り開始%s" % (name, "（再生/一時停止あり）" if info.has_play_pause else ""))
    counts[name] = 0
    last = None
    try:
        while not stop.is_set():
            rep = src.read_report()
            if rep is None:
                break
            b = bytes(rep[:24])
            counts[name] += 1
            if b != last:
                out("[%s] %s%s" % (name, b.hex(" "), " …" if len(rep) > 24 else ""))
                last = b
    except Exception as e:
        out("[%s] 読み取りが止まりました: %s" % (name, e))
    finally:
        try:
            src.close()
        except Exception:
            pass


def main():
    infos = [i for i in D.list_hid_infos(VID_PID) if D.path_matches(i.path, *D.parse_vid_pid(VID_PID))]
    out("HID インターフェース %d 件: %s" % (len(infos), ", ".join(D.short_path(i.path) for i in infos)))
    ths = [threading.Thread(target=watch, args=(i,), daemon=True) for i in infos]
    for t in ths:
        t.start()
    out("準備OK。いまから %d 秒間、ボタンを3秒以上あけて押してください（終了は Ctrl+C）" % SECONDS)
    try:
        t0 = time.time()
        while time.time() - t0 < SECONDS:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    stop.set()
    out("終了。届いたレポートの件数: %s" % counts)
    out("ログ: %s" % LOG)
    time.sleep(1.0)
    import os
    os._exit(0)


if __name__ == "__main__":
    main()
