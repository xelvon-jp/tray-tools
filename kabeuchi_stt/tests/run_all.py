# -*- coding: utf-8 -*-
"""全テストを実行する（PowerShell から）。  python tests\\run_all.py [--fast]
--fast: 実モデルを使う e2e を除く。マイクは開かない・窓は画面に出さない・音は鳴らさない。
モジュールごとに別プロセスで走らせる（Qt と CTranslate2 を同じプロセスに入れるとハングしたため）。
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
names = ["test_core_mic", "test_core_data", "test_core_runtime", "test_core_v15", "test_core_v16",
         "test_gui_smoke", "test_gui_v15", "test_gui_v16"]
if "--fast" not in sys.argv:
    names.append("test_e2e")
t0 = time.time()
total, bad = 0, 0


def run_module(n):
    env = dict(os.environ, KABEUCHI_TEST_TIMEOUT="150")   # 固まったら 150 秒でスタックを出して強制終了
    return subprocess.run([sys.executable, str(here / f"{n}.py")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, cwd=str(here), env=env)


for n in names:
    t1 = time.time()
    try:
        r = run_module(n)
        if r.returncode != 0 and "Timeout (" in r.stderr:
            # Qt の再生（QMediaPlayer.stop）がまれに環境要因で固まる。固まった（faulthandler が働いた）ときだけ
            # 1 回再実行する。通常の失敗（assert 等）は再実行しない
            print(f"{n}: 固まったので 1 回だけ再実行します（{time.time() - t1:.0f}秒）")
            print(r.stderr[-1200:])
            r = run_module(n)
    except subprocess.TimeoutExpired as e:
        bad += 1
        print(f"{n}: タイムアウト NG")
        print(str(e.stderr or "")[-3000:])
        continue
    m = re.search(r"Ran (\d+) tests?", r.stderr)
    cnt = int(m.group(1)) if m else 0
    total += cnt
    ok = r.returncode == 0
    bad += 0 if ok else 1
    print(f"{n}: {cnt} 本 {'OK' if ok else 'NG'} ({time.time() - t1:.0f}秒)")
    if not ok:
        print(r.stderr[-3000:])
print(f"\n合計 {total} 本 / {time.time() - t0:.0f} 秒 / NG モジュール {bad}")
sys.exit(1 if bad else 0)
