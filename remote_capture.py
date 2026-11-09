# remote_capture.py
# リモート撮影。リポジトリ内の合言葉ファイル(remote_capture.json)の nonce が
# 新しくなっていたら、常駐の起動時に1回だけ画面全体を撮って保存する。
#
# 【なぜこんな形か】
# ARiA(郵便受け・窓口・配備係)の実験として、席を離れている間にスマホから
# 「いまの PC の画面を見せて」と頼めるようにするための仕組み。PC 側でモデルを
# 使わずに遠隔で1回だけ発火させる口が要るが、今の時点で PC に届けられる
# 変更は窓口のプレイブック PB-1(tray-tools 本体へ main を取り込んで再起動)だけ。
# そこで合図も main に載せる: 合言葉ファイルの nonce を変えた push を PB-1 が
# 取り込むと、再起動した常駐が新しい合言葉を見て撮影する。
#
# 【安全側の決めごと】
# - 保存先は %OneDrive%\traytools-remote-capture 固定。合言葉ファイルには
#   保存先も撮影範囲も書けない(書けるようにすると、リポジトリへの書き込みが
#   そのまま PC への任意の書き出しになるため)。OneDrive が無ければ通常の
#   キャプチャ保存先(settings の capture.save_folder)に落とす。
# - 画像は git に入らない。OneDrive の同期でスマホから見る前提。
# - nonce は「消費済み」を %LOCALAPPDATA%\traytools\remote_capture_seen.json に
#   記録し、同じ合言葉では二度撮らない。記録は撮影より先に行う(途中で落ちても
#   再起動のたびに撮り直す連射にならない)。
# - Muse アプリへの貼り付けは別プロセス(remote_capture_paste.py)に任せる。
#   常駐の中に UIA / COM を持ち込まない(copilot_loop._ensure_uia の事故歴と
#   tests/test_com_gc.py の見張り対象を参照)。こちらは起動して見届けない。
# - 何があっても例外を起動側(main)へ投げない。失敗は action_log に残すだけ。
import json
import os
import subprocess
import threading
import sys
from pathlib import Path

import action_log

TRIGGER_NAME = "remote_capture.json"
SEEN_NAME = "remote_capture_seen.json"
FOLDER_NAME = "traytools-remote-capture"
PASTE_SCRIPT_NAME = "remote_capture_paste.py"

# 親(常駐)が終わっても道連れにならないよう切り離して起動する
# (traytools_send.py と同じ定数)。
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def app_data_dir(environ=None) -> Path:
    """%LOCALAPPDATA%\\traytools。見張りの合図ファイル等と同じ置き場所。"""
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "traytools"


def read_trigger_nonce(path):
    """合言葉ファイルから nonce を読む。無い・空・壊れているはすべて None。

    「撮らない」側に倒すための読み方。ファイルが無いのは通常運用(この機能を
    使っていない)でもあり得るので、エラーにはしない。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    nonce = data.get("nonce")
    if not isinstance(nonce, str):
        return None
    nonce = nonce.strip()
    return nonce or None


def load_seen_nonce(path):
    """消費済みの nonce を読む。読めなければ None(=まだ何も消費していない)。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("nonce"), str):
        return data["nonce"] or None
    return None


def save_seen_nonce(path, nonce) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"nonce": nonce}, ensure_ascii=False) + "\n", encoding="utf-8")


def pending_nonce(trigger_path, seen_path):
    """新しくて未消費の合言葉があればそれを返す。無ければ None。"""
    nonce = read_trigger_nonce(trigger_path)
    if nonce is None:
        return None
    if load_seen_nonce(seen_path) == nonce:
        return None
    return nonce


def resolve_target_folder(environ=None):
    """保存先。%OneDrive%\\traytools-remote-capture。OneDrive が無ければ None。"""
    env = os.environ if environ is None else environ
    one_drive = (env.get("OneDrive") or "").strip()
    if not one_drive:
        return None
    return Path(one_drive) / FOLDER_NAME


def check_and_run(app_settings, *, base_dir=None, environ=None) -> str:
    """起動時の入口。新しい合言葉があれば撮影し、結果を短い文字列で返す。

    戻り値は "no_trigger" / "captured" / "failed:..."。
    例外は外へ出さない(常駐の起動を止めないため)。"""
    try:
        return _run(app_settings, base_dir=base_dir, environ=environ)
    except Exception as e:  # noqa: BLE001  起動側に投げないための最後の受け皿
        action_log.record("リモート撮影 失敗", f"{type(e).__name__}: {e}", "remote")
        return f"failed:{type(e).__name__}"


def _run(app_settings, *, base_dir=None, environ=None) -> str:
    base = Path(__file__).resolve().parent if base_dir is None else Path(base_dir)
    seen_path = app_data_dir(environ) / SEEN_NAME
    nonce = pending_nonce(base / TRIGGER_NAME, seen_path)
    if nonce is None:
        # 合言葉が無いのか消費済みなのかは、呼び出し側の関心ではないので分けない。
        return "no_trigger"

    # 撮影より先に消費を記録する(落ちても連射しないための順序)。
    save_seen_nonce(seen_path, nonce)

    # Qt と capture_grab はここで初めて読む(純粋な判定部分をテストや他環境で
    # import できるようにするため)。
    from PySide6.QtWidgets import QApplication

    import capture_grab

    if not QApplication.screens():
        action_log.record("リモート撮影 失敗", "画面が見つかりません", "remote")
        return "failed:no_screen"

    folder = resolve_target_folder(environ)
    if folder is not None:
        capture_settings = {"save_folder": str(folder), "save_format": "png"}
    else:
        capture_settings = dict((app_settings or {}).get("capture", {}))

    image = capture_grab.grab_region(
        capture_grab.virtual_geometry(), include_layered=True)
    if image.isNull():
        action_log.record("リモート撮影 失敗", "撮影できませんでした(空の画像)", "remote")
        return "failed:empty_image"

    path = capture_grab.save_image(
        image, capture_settings, stem=capture_grab.new_session_stem())
    if not path:
        action_log.record("リモート撮影 失敗", "保存できませんでした", "remote")
        return "failed:save"

    action_log.record(
        "リモート撮影", f"{image.width()}x{image.height()} {path}", "remote")
    _ring_pushover(Path(path))
    _spawn_paste_helper(base, Path(path))
    return "captured"


def _ring_pushover(image_path: Path) -> None:
    """撮れたことを Pushover でスマホへ知らせる(呼び鈴)。

    pushover.send は数秒かかりうるので別スレッド(main の _pushover_command と
    同じ作法)。未登録なら何もしない。失敗しても撮影自体は成功として扱う。"""
    try:
        import pushover

        if not pushover.is_registered():
            return
    except Exception:  # noqa: BLE001  呼び鈴は予備。落としても本筋に触らない
        return

    def work():
        try:
            pushover.send(
                f"画面を撮りました: {image_path.name}",
                title="ARiA リモート撮影")
        except Exception:  # noqa: BLE001  send は例外を投げない約束だが念のため
            pass

    threading.Thread(target=work, name="remote-capture-pushover", daemon=True).start()


def _spawn_paste_helper(base: Path, image_path: Path) -> None:
    """貼り付け係(remote_capture_paste.py)を別プロセスで起動する。

    見届けない。成否は向こうが action_log と
    %LOCALAPPDATA%\\traytools\\remote_capture_last.json に残す。"""
    script = base / PASTE_SCRIPT_NAME
    try:
        subprocess.Popen(
            [sys.executable, str(script), str(image_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as e:
        action_log.record("リモート撮影 投稿の起動に失敗", str(e)[:80], "remote")
