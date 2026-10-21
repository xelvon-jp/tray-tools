"""COM ポインタが循環 GC に回ってくる経路を、常駐を使わずに叩き出す器(ハーネス)。

常駐が使っている COM の読み取り経路(音量・既定デバイス・デバイス列挙・マイクのミュート
状態・エクスプローラの窓列挙)を、常駐と同じスレッド構成で繰り返し叩き、合間に小さな
循環を大量に作って GC を誘発する。

    メインスレッド         … Qt のメインスレッドの代わり(feature_audio._refresh と同じ読み)
    keyboard 役のスレッド   … ホットキーのコールバックと同じく、自分で CoInitialize して読む
    読み役スレッド          … 同上(別スレッドで COM を作るものの代表)

**読み取りしかしない。** 音量・既定デバイス・ミュートを変える関数は呼ばない。キー送信・
マウス操作・窓の前面化もしない。

使い方(venv の python で):
    python tools/com_gc_harness.py --probe              調査モード。com_gc_probe.log に記録
    python tools/com_gc_harness.py --seconds 20          調査モードなし(落ちるかどうかを見る)
    python tools/com_gc_harness.py --only mic --probe    経路を絞る(mic/audio/list/explorer/uia)
    python tools/com_gc_harness.py --only mic_legacy     直す前の mic_control の書き方(落ちる)

終了コード: 0 = 最後まで走った。access violation なら Windows の 0xC0000005(3221225477)。
"""
import argparse
import gc
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import com_gc_probe  # noqa: E402

ALL_PATHS = ("mic", "audio", "list", "explorer", "uia", "mic_legacy")


def _legacy_mic_get_mute():
    """2026-10-21 まで mic_control にあった書き方(ctypes.cast で型を変える)。再現用。

    cast は AddRef せず、元のポインタを自分の _objects に抱えさせる(循環)。cast の結果が
    先に Release して COM オブジェクトが消え、残った元のポインタを循環 GC が後から
    Release して落ちる。常駐の crash.log と同じスタックになる。**直した版と比べる以外に使わない。**"""
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.api.endpointvolume import IAudioEndpointVolume
    from pycaw.utils import AudioUtilities

    microphone = AudioUtilities.GetMicrophone()
    if microphone is None:
        return None
    interface = microphone.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    return bool(volume.GetMute())


def _churn(n=2000):
    """小さな循環を作って捨てる。gen0 の閾値を越えさせて GC を走らせるため。"""
    for _ in range(n):
        a = []
        a.append(a)


def _make_readers(paths):
    readers = []
    if "mic" in paths:
        import mic_control

        readers.append(("mic.get_mute", mic_control.get_mute))
    if "audio" in paths or "list" in paths:
        import feature_audio
    if "audio" in paths:
        current = feature_audio._get_current_device_id()

        readers.append(("audio._current_volume", feature_audio._current_volume))
        readers.append(("audio._get_current_device_id", feature_audio._get_current_device_id))
        readers.append(("audio._volume_step", feature_audio._volume_step))
        if current:
            readers.append(("audio._device_exists", lambda: feature_audio._device_exists(current)))
            readers.append(("audio._friendly_name", lambda: feature_audio._friendly_name(current)))
    if "list" in paths:
        readers.append(("audio._list_output_devices", feature_audio._list_output_devices))
    if "explorer" in paths:
        import explorer_nav

        # 実在しない HWND を渡す。Shell.Application の窓列挙だけを通し、どの窓にも触らない。
        readers.append(("explorer.explorer_path", lambda: explorer_nav.explorer_path(1)))
    if "uia" in paths:
        readers.append(("uia.root_name", _uia_root_name))
    if "mic_legacy" in paths:
        readers.append(("mic_legacy(cast)", _legacy_mic_get_mute))
    return readers


def _uia_root_name():
    """UIA でデスクトップの名前を読むだけ(copilot_status_process が子プロセスでやる経路の代表)。"""
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    comtypes.client.GetModule("UIAutomationCore.dll")
    import comtypes.gen.UIAutomationClient as uia_mod

    uia = comtypes.client.CreateObject(uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation)
    root = uia.GetRootElement()
    return root.CurrentName


def _thread_main(readers, stop, errors, name):
    import comtypes

    comtypes.CoInitialize()
    try:
        while not stop.is_set():
            for label, fn in readers:
                try:
                    fn()
                except Exception as e:  # 読み取りの失敗は数えるだけ
                    errors.append(f"{name} {label}: {e!r}")
                _churn(300)
    finally:
        # 常駐の keyboard スレッドは CoUninitialize しない。ここでも呼ばない(同じ条件にする)。
        pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="調査モードを有効にする")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--only", default=",".join(p for p in ALL_PATHS if p not in ("uia", "mic_legacy")),
                    help="叩く経路(カンマ区切り): " + ",".join(ALL_PATHS))
    ap.add_argument("--threads", type=int, default=2, help="メイン以外に回すスレッドの数")
    args = ap.parse_args(argv)
    paths = {p.strip() for p in args.only.split(",") if p.strip()}

    if args.probe:
        if not com_gc_probe.enable():
            print("調査モードを入れられませんでした", file=sys.stderr)
            return 2

    import comtypes

    comtypes.CoInitialize()
    readers = _make_readers(paths)
    stop = threading.Event()
    errors = []
    threads = [
        threading.Thread(target=_thread_main, args=(readers, stop, errors, f"worker{i}"),
                         name=("keyboard-like" if i == 0 else f"reader-{i}"), daemon=True)
        for i in range(args.threads)
    ]
    for t in threads:
        t.start()

    deadline = time.monotonic() + args.seconds
    loops = 0
    last_scan = time.monotonic()
    while time.monotonic() < deadline:
        for label, fn in readers:
            try:
                fn()
            except Exception as e:
                errors.append(f"main {label}: {e!r}")
            _churn(300)
        loops += 1
        if args.probe and time.monotonic() - last_scan > 1.0:
            com_gc_probe.scan()
            last_scan = time.monotonic()
    stop.set()
    for t in threads:
        t.join(timeout=10)
    gc.collect()
    found = com_gc_probe.scan() if args.probe else 0
    print(f"loops(main)={loops} paths={sorted(paths)} threads={args.threads} "
          f"errors={len(errors)} probe={com_gc_probe.counts() if args.probe else '-'} last_scan_found={found}")
    for e in errors[:5]:
        print("  ", e)
    sys.stdout.flush()
    # 調査モードでは gc.garbage と止めた Release を抱えたまま終わる。atexit の CoUninitialize 後に
    # 解放されないよう、ここで即座に抜ける(調査用のリーク。プロセスと一緒に消える)。
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
