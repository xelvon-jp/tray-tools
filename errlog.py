# errlog.py
# 例外の記録を1か所に寄せたもの。依存は標準ライブラリだけ(Qt も toast も
# 直接は触らない。通知は notify_hook 経由)。
#
# 背景: 各モジュールが _guard を個別に持っていたが、どれも標準エラーへ出す
# だけで、通常起動(pythonw.exe)ではどこにも残らなかった。捕まえた例外は
# ここを通して error.log(main._install_excepthook と同じ置き場・同じ形)へ
# 必ず残す。main.log_exception を呼べない理由だった import の循環
# (main → feature_screen → 各モジュール)も、これなら起きない。
import gc
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ERROR_LOG_PATH = Path(__file__).resolve().parent / "error.log"

# 同じ (component, where) の通知を繰り返さない間隔(秒)。周期タイマーから
# 同じ失敗が続いても、トーストはこの間隔で最大1回だけ出す。
NOTIFY_MIN_INTERVAL = 60.0

# 通知の宛先。callable(component, where) -> None。main が起動時に toast へ
# 繋ぐ。繋がっていない間(単体テストや子プロセス)は通知しない。
notify_hook = None

_last_notified: dict = {}


def _format_exc() -> str:
    """いま処理中の例外を文字列にする。整形の間は GC を止める。

    main._install_excepthook の formatted と同じ理由: 整形の最中に GC が
    走ると、comtypes の COM オブジェクトが別スレッドから解放されて
    プロセスごと落ちることがある(記録しようとして死ぬのは本末転倒)。"""
    gc.disable()
    try:
        return traceback.format_exc()
    finally:
        gc.enable()


def _append(header: str, text: str) -> None:
    """error.log へ main と同じ形で追記する。書けなくても例外にしない。"""
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {header} =====\n{text}")
            if not text.endswith("\n"):
                f.write("\n")
    except OSError:
        pass


def _notify_allowed(component: str, where: str) -> bool:
    key = (component, where)
    now = time.monotonic()
    last = _last_notified.get(key)
    if last is not None and now - last < NOTIFY_MIN_INTERVAL:
        return False
    _last_notified[key] = now
    return True


def guard(component: str, where: str, notify: bool = True, title: str = None) -> None:
    """except の中で呼び、例外を error.log と標準エラーへ残す。

    component は出す側の名前(「画面ミラー」など、標準エラーの行とトーストの
    見出しに使う)。title を渡すとトーストの見出しだけそちらを使う。
    notify=False は周期タイマー・描画用(記録はするが通知しない)。
    ここ自体は例外を投げない(例外処理の途中で落ちると元の原因が消える)。
    """
    text = _format_exc()
    _append(f"{component}: {where}", text)
    print(text, file=sys.stderr)
    print(f"[tray-tools] {component}: {where}に失敗しました", file=sys.stderr)
    if notify and notify_hook is not None and _notify_allowed(component, where):
        try:
            notify_hook(title or component, where)
        except Exception:  # noqa: BLE001  通知の失敗で本流を止めない
            pass


def note(component: str, where: str, message: str) -> None:
    """例外ではない記録を error.log へ残す。

    「except: pass で握るが、起きたことだけは残したい」場所用。"""
    _append(f"{component}: {where}", message)
    print(f"[tray-tools] {component}: {where}: {message}", file=sys.stderr)
