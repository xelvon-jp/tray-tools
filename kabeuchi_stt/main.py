# -*- coding: utf-8 -*-
"""壁打ち文字起こし v2 の入口（PySide6）。

  GUI（トレイ常駐）  : python main.py
  清書のやり直し      : python main.py --wav out\\xxx.wav [--model medium]   （GUIなし）
  デバイス一覧        : python main.py --list-devices                         （マイクは開かない）
  窓を出さず常駐のみ  : python main.py --hidden
旧 v1（tkinter）は app.py のまま残してある。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PYTHONUTF8", "1")

from settings import load_settings  # noqa: E402
from version import VERSION  # noqa: E402


def run_gui(settings: dict, hidden: bool = False, no_tray: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    from services import build_services, setup_error_log
    from single_instance import SingleInstance, notify_existing
    from ui_main import Bridge, MainWindow
    import ui_style as S
    from ui_style import make_icon

    from winshell import set_app_user_model_id
    set_app_user_model_id()  # 専用の AppUserModelID（タスクバーで python のアイコンにまとまらない）。窓を作る前に
    log = setup_error_log()
    app = QApplication(sys.argv)
    app.setApplicationName("壁打ち文字起こし")
    S.set_size(str(settings.get("display_size", S.DEFAULT_SIZE)))
    app.setFont(S.app_font())
    app.setStyleSheet(S.QSS)
    app.setWindowIcon(make_icon())
    # トレイ常駐のときは窓を閉じても終わらない。--no-tray（tray-tools から起こす形）では、窓を閉じたら
    # プロセスも終わる（取り残されて見えないまま CPU・マイクを握り続けないため）。
    app.setQuitOnLastWindowClosed(bool(no_tray))

    if notify_existing():  # 2つ目の起動: 既存の窓を出して終わる
        return 0

    bridge = Bridge()
    svc = build_services(settings, on_job_event=lambda j: bridge.job_event.emit(j),
                         on_status=lambda m: bridge.status.emit(m))
    win = MainWindow(svc, bridge, use_tray=not no_tray)
    single = SingleInstance(lambda msg: win.show_main())
    if not single.listen():
        log.error("単一起動のサーバーを立てられませんでした")
    win.apply_hotkeys()  # 既定は未割り当て（設定の hotkey_record / hotkey_show で指定）
    if not hidden or no_tray:
        win.show()
    code = app.exec()
    try:
        svc.session.close()
    except Exception:
        log.exception("終了時の入力ストリームの解放")
    svc.queue.close()
    return code


def cli_wav(wav: str, model: str | None, settings: dict) -> int:
    """清書だけやり直す（GUIなし）。置換辞書を適用する。履歴には登録しない。"""
    from replacements import Replacer, load_pairs
    from transcriber import transcribe_wav
    last = [-1]

    def prog(p):
        pct = int(p * 100)
        if pct // 10 != last[0]:
            last[0] = pct // 10
            print(f"  進捗 {pct}%", flush=True)
    from paragraph import format_for_output
    r = transcribe_wav(Path(wav), settings, model_name=model, on_progress=prog,
                       postprocess=Replacer(load_pairs()).apply,
                       formatter=lambda segs: format_for_output(segs, settings))
    print(r.text)
    print(f"\n保存: {r.txt_path}\n保存: {r.md_path}\n所要 {r.elapsed:.1f}秒 / 音声 {r.audio_seconds:.1f}秒")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"壁打ち文字起こし v2 {VERSION}")
    ap.add_argument("--wav", help="既存の WAV を清書だけやり直す（GUIなし）")
    ap.add_argument("--model", help="清書モデルを一時的に変更（small / medium / large-v3）")
    ap.add_argument("--list-devices", action="store_true", help="入力デバイス一覧（マイクは開かない）")
    ap.add_argument("--hidden", action="store_true", help="窓を出さずトレイ常駐で起動")
    ap.add_argument("--no-tray", action="store_true", help="トレイを使わない（閉じると終了）")
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
            return cli_wav(a.wav, a.model, settings)
        except ModelUnavailableError as e:
            print(e)
            return 2
    return run_gui(settings, hidden=a.hidden, no_tray=a.no_tray)


if __name__ == "__main__":
    sys.exit(main())
