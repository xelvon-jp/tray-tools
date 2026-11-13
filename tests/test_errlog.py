# test_errlog.py
# errlog(例外の記録の一本化)と、filer のメッセージログのファイル控えのテスト。
# 実ファイルは一時フォルダで扱う。
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import errlog  # noqa: E402


def _with_log_path(fn):
    """errlog.ERROR_LOG_PATH を一時ファイルへ差し替えて fn(path) を呼ぶ。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "error.log"
        original = errlog.ERROR_LOG_PATH
        errlog.ERROR_LOG_PATH = path
        errlog._last_notified.clear()
        try:
            fn(path)
        finally:
            errlog.ERROR_LOG_PATH = original
            errlog._last_notified.clear()


def test_guard_writes_traceback_to_error_log():
    """except の中で呼ぶと、見出しと例外の中身が error.log に残る。"""
    def run(path):
        try:
            raise ValueError("boom-123")
        except ValueError:
            errlog.guard("画面ミラー", "枠の表示の更新", notify=False)
        text = path.read_text(encoding="utf-8")
        assert "画面ミラー: 枠の表示の更新" in text, text
        assert "ValueError: boom-123" in text, text

    _with_log_path(run)


def test_guard_notify_hook_and_rate_limit():
    """通知はフック経由で、同じ (component, where) は間引かれる。"""
    def run(path):
        calls = []
        original_hook = errlog.notify_hook
        errlog.notify_hook = lambda title, where: calls.append((title, where))
        try:
            for _ in range(3):
                try:
                    raise RuntimeError("x")
                except RuntimeError:
                    errlog.guard("ランチャ", "描画", title="タスクバーのランチャ")
            assert calls == [("タスクバーのランチャ", "描画")], calls
            # notify=False は通知しない
            try:
                raise RuntimeError("x")
            except RuntimeError:
                errlog.guard("ランチャ", "別の場所", notify=False)
            assert len(calls) == 1, calls
            # 別の where は間引かれない
            try:
                raise RuntimeError("x")
            except RuntimeError:
                errlog.guard("ランチャ", "別の場所")
            assert calls[-1] == ("ランチャ", "別の場所"), calls
        finally:
            errlog.notify_hook = original_hook

    _with_log_path(run)


def test_note_appends_message():
    """例外でない記録も同じ置き場に残る。"""
    def run(path):
        errlog.note("見張り", "読み取り", "合図ファイルを読みました")
        text = path.read_text(encoding="utf-8")
        assert "見張り: 読み取り" in text, text
        assert "合図ファイルを読みました" in text, text

    _with_log_path(run)


def test_guard_never_raises_when_log_unwritable():
    """ログが書けない場所でも guard 自体は例外を投げない。"""
    with tempfile.TemporaryDirectory() as td:
        original = errlog.ERROR_LOG_PATH
        errlog.ERROR_LOG_PATH = Path(td) / "no" / "such" / "dir" / "error.log"
        try:
            try:
                raise RuntimeError("x")
            except RuntimeError:
                errlog.guard("部品", "場所", notify=False)  # 例外にならないこと
        finally:
            errlog.ERROR_LOG_PATH = original


def test_filer_message_log_append_and_rotation():
    """filer のメッセージ控え: 追記され、上限を超えたら .1 へ退避する。"""
    try:
        import filer
    except Exception as e:  # noqa: BLE001  WinDLL 等、Windows 前提の import が要る
        print(f"skip  filer を import できない環境: {e}")
        return

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "traytools" / "filer.log"
        original_path = filer.MESSAGE_LOG_PATH
        original_max = filer.MESSAGE_LOG_MAX_BYTES
        filer.MESSAGE_LOG_PATH = path
        filer.MESSAGE_LOG_MAX_BYTES = 10
        try:
            filer._append_message_log("1行目")
            assert "1行目" in path.read_text(encoding="utf-8")
            path.write_text("x" * 20, encoding="utf-8")  # 上限超えの状態にする
            filer._append_message_log("2行目")
            rotated = path.with_name("filer.log.1")
            assert rotated.exists()
            assert rotated.read_text(encoding="utf-8") == "x" * 20
            assert "2行目" in path.read_text(encoding="utf-8")
        finally:
            filer.MESSAGE_LOG_PATH = original_path
            filer.MESSAGE_LOG_MAX_BYTES = original_max


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
