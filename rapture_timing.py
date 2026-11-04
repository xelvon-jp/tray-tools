# rapture_timing.py
# Rapture の「範囲を選んでから付箋が描き終わるまで」の所要時間を rapture_timing.log へ残す。
#
# なぜ要るのか
#   「範囲を選んで、ウインドウが出てから絵が出るまで数秒かかることがある」という報告に
#   対し、コードを読むだけでは原因を1つに絞れなかった(2026-11-04)。候補は3つあり、
#   どこで時間を使ったかが分かれば見分けられる:
#     1. 長く使われなかった待機役がメモリから追い出されていて、読み戻しに時間がかかる
#        → 付箋側の「表示→初回描画」が長く、待機時間(idle)も長い
#     2. 待機役が居ないので、その場でプロセスを起こした(spawn)
#        → 本体側の経路が spawn、付箋側の「起動」が長い
#     3. 本体の画面処理の中で、自動保存や受け渡し用 PNG の書き出しが詰まった
#        → 本体側の「保存」「受渡PNG」が長い
#
# 書き方
#   本体(main.py の中の feature_screen)と付箋(capture_process.py)の別プロセスが、
#   同じファイルへ1行ずつ追記する。2行は session_stem(rapture_20261104_…)で対応が取れる。
#   時刻は両プロセスで比べられるよう time.time()(エポック ms)で持つ。
#
#   Qt に依存しない。失敗しても何もしない(計測のせいでキャプチャが失敗しては本末転倒)。
#   512KB を超えたら .1 へ退避して新しく始める(1回のキャプチャで2行なので、数千回分)。
import os
import time
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "rapture_timing.log"
MAX_BYTES = 512 * 1024


def now_ms() -> float:
    """両プロセスで比べられる時刻(エポックからの ms)。"""
    return time.time() * 1000.0


def span(start_ms, end_ms) -> str:
    """2つの時刻の差を "123ms" で。どちらか無ければ "-"。"""
    if start_ms is None or end_ms is None:
        return "-"
    return f"{end_ms - start_ms:.0f}ms"


def write(kind: str, session_stem, fields: dict, path=None) -> None:
    """1行追記する。kind は "本体" / "付箋"。fields は 名前→値 の並び(順序を保つ)。"""
    try:
        target = Path(path) if path is not None else LOG_PATH
        try:
            if target.stat().st_size > MAX_BYTES:
                os.replace(target, target.with_name(target.name + ".1"))
        except OSError:
            pass
        parts = [f"{datetime.now():%Y-%m-%d %H:%M:%S}", kind, str(session_stem or "-")]
        parts += [f"{key}={value}" for key, value in fields.items()]
        with open(target, "a", encoding="utf-8", newline="") as f:
            f.write(" ".join(parts) + "\n")
    except Exception:  # noqa: BLE001  計測の失敗でキャプチャを止めない
        pass
