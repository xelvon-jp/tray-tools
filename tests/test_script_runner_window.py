# tests/test_script_runner_window.py
# 「スクリプト実行パネル」の窓(script_runner.py)と入口の配線のテスト。
# チェックの判定そのものは test_script_runner.py が見る。ここでは、窓が
# 一覧を作ってチェック結果を並べること、上書きで再チェックになること、
# F5 の出し分け(問題なしは実行・エラーは実行不可・要確認は F5 では
# 実行しない)、結果がクリップボード文の形で入ること、設定が窓の位置と
# 前面固定を覚えること、入口(トレイ・外部コマンド・あっふ)の配線が
# あることを見る。
#
# 設定は一時ファイル、見張りフォルダも一時フォルダ。実 settings.json には
# 触らない。クリップボードは offscreen で不安定なので、窓の _set_clipboard
# を差し替えて受け取る(窓側の _last_clipboard_text も合わせて立てる)。
#
#   python tests/test_script_runner_window.py
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

import settings as settings_mod  # noqa: E402
import script_runner as sr  # noqa: E402
import script_runner_core as core  # noqa: E402

APP = QApplication.instance() or QApplication([])


def pump_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def make_window(tmp: Path):
    watch = tmp / "watch"
    watch.mkdir()
    settings_path = tmp / "settings.json"
    data = json.loads(json.dumps(settings_mod.DEFAULT_SETTINGS))
    data["script_runner"] = {
        "watch_folder": str(watch),
        "python": sys.executable,
        "timeout_seconds": 10,
    }
    settings_path.write_text(json.dumps(data, ensure_ascii=False),
                             encoding="utf-8")
    window = sr.ScriptRunnerWindow(str(settings_path))
    copied: list[str] = []

    def fake_clipboard(text):
        copied.append(text)
        window._last_clipboard_text = text

    window._set_clipboard = fake_clipboard  # type: ignore[method-assign]
    return window, watch, settings_path, copied


def write_script(watch: Path, name: str, content: str) -> Path:
    path = watch / name
    path.write_text(content, encoding="utf-8", newline="")
    return path


def item_of(window, name: str):
    for item in window._items:
        if item.name == name:
            return item
    return None


def settle_checks(window, names):
    def done():
        return all(
            (item := item_of(window, name)) is not None
            and item.result is not None
            for name in names)
    assert pump_until(done), f"チェックが終わらない: {names}"


def select(window, name: str):
    for index, item in enumerate(window._items):
        if item.name == name:
            window.list.setCurrentRow(index)
            QCoreApplication.processEvents()
            return item
    raise AssertionError(f"一覧に無い: {name}")


def row_button_texts(window, name: str):
    for index, item in enumerate(window._items):
        if item.name == name:
            entry = window.list.item(index)
            widget = window.list.itemWidget(entry)
            if widget is None:
                return []
            return [b.text() for b in widget.findChildren(QPushButton)]
    return []


def test_window_lists_and_checks():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, _copied = make_window(tmp)
        try:
            write_script(watch, "ok.py", "print('hi')\n")
            write_script(watch, "bad.py", "x = (1\n")
            write_script(watch, "warn.py", "import os\nos.remove('x')\n")
            window.show()
            settle_checks(window, ["ok.py", "bad.py", "warn.py"])
            assert item_of(window, "ok.py").result.status == core.STATUS_OK
            assert item_of(window, "bad.py").result.status == \
                core.STATUS_ERROR
            assert item_of(window, "warn.py").result.status == \
                core.STATUS_WARN
            # 新しい順(後に書いたものが上)に並ぶ。
            order = [item.name for item in window._items]
            assert order[0] == "warn.py", order
            # 選んだ行だけにボタンが出る。
            select(window, "ok.py")
            texts = row_button_texts(window, "ok.py")
            assert "実行 F5" in texts and "EDDy で開く" in texts, texts
            assert row_button_texts(window, "bad.py") == []
            select(window, "bad.py")
            texts = row_button_texts(window, "bad.py")
            assert "エラーをコピー" in texts, texts
            assert not any("実行" in t for t in texts), texts
            select(window, "warn.py")
            texts = row_button_texts(window, "warn.py")
            assert "中身を見てから実行" in texts, texts
        finally:
            window.close()


def test_f5_gating_and_run_copies_result():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, copied = make_window(tmp)
        try:
            write_script(watch, "ok.py", "print('結果です')\n")
            write_script(watch, "warn.py", "import os\nos.remove('x')\n")
            window.show()
            settle_checks(window, ["ok.py", "warn.py"])
            # 要確認: F5 では実行しない(下半分に理由が出るだけ)。
            select(window, "warn.py")
            window._f5_pressed()
            assert window._running_item is None
            assert copied == []
            assert "os.remove" in window.output.toPlainText()
            # 「中身を見てから実行」で該当行が出て、もう一度押すと実行する。
            item = item_of(window, "warn.py")
            window._view_and_run(item)
            assert "os.remove" in window.output.toPlainText()
            texts = row_button_texts(window, "warn.py")
            assert "確認しました。実行する" in texts, texts
            window._view_and_run(item)
            assert pump_until(lambda: window._running_item is None
                              and copied != [])
            assert copied[-1].startswith("[実行結果] warn.py"), copied[-1]
            assert item_of(window, "warn.py").ran
            # 問題なし: F5 で実行し、結果文がクリップボードへ。
            select(window, "ok.py")
            copied.clear()
            window._f5_pressed()
            assert pump_until(lambda: copied != []
                              and window._running_item is None)
            text = copied[-1]
            assert text.startswith("[実行結果] ok.py  終了コード 0"), text
            assert "結果です" in text
            assert "クリップボードに入れました" in window.note.text()
            # Ctrl+C で直前の結果をもう一度入れる。
            copied.clear()
            window._copy_last()
            assert copied == [text]
        finally:
            window.close()


def test_error_copy_text():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, copied = make_window(tmp)
        try:
            write_script(watch, "bad.py",
                         "counts = {}\n"
                         "rows = sorted(counts.items(), key=lambda x: x[1]\n")
            window.show()
            settle_checks(window, ["bad.py"])
            item = select(window, "bad.py")
            window._copy_check_error(item)
            assert copied, "エラーの文が入っていない"
            text = copied[-1]
            assert text.startswith("[実行前チェック] bad.py  文法エラー"), text
            assert "2行目:" in text
            assert text.rstrip().endswith("もう一度ください。")
        finally:
            window.close()


def test_overwrite_rechecks_and_clears_ran():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, copied = make_window(tmp)
        try:
            path = write_script(watch, "chg.py", "print('v1')\n")
            window.show()
            settle_checks(window, ["chg.py"])
            select(window, "chg.py")
            window._f5_pressed()
            assert pump_until(lambda: copied != []
                              and window._running_item is None)
            assert item_of(window, "chg.py").ran
            old_sha = item_of(window, "chg.py").result.sha256
            # 上書きしたら再チェックになり、実行済みの印が外れる。
            path.write_text("import os\nos.remove('x')\n",
                            encoding="utf-8", newline="")
            assert pump_until(
                lambda: item_of(window, "chg.py").result is not None
                and item_of(window, "chg.py").result.sha256 != old_sha)
            item = item_of(window, "chg.py")
            assert item.result.status == core.STATUS_WARN
            assert not item.ran
        finally:
            window.close()


def test_run_aborts_when_file_changed_after_check():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, copied = make_window(tmp)
        try:
            path = write_script(watch, "swap.py", "print('safe')\n")
            window.show()
            settle_checks(window, ["swap.py"])
            select(window, "swap.py")
            # チェック後に中身が差し替わった状態を直接作る(ポーリングが
            # 拾う前に F5 を押した競合の再現)。この間イベントは回さない。
            path.write_text("print('swapped')\n", encoding="utf-8",
                            newline="")
            copied.clear()
            window._f5_pressed()
            assert pump_until(
                lambda: item_of(window, "swap.py").result is not None)
            # 実行されず、チェックからやり直しになった。
            assert all("[実行結果]" not in text for text in copied)
            assert "変わっていた" in window.note.text()
        finally:
            window.close()


def test_window_settings_saved():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, _watch, settings_path, _copied = make_window(tmp)
        try:
            window.show()
            QCoreApplication.processEvents()
            window.move(123, 45)
            window.resize(333, 555)
            window.on_top_box.setChecked(True)
            window.close()
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            section = saved["script_runner"]
            assert section["window_geometry"] == [123, 45, 333, 555], section
            assert section["always_on_top"] is True
            # 他の節は壊れていない。
            assert "editor" in saved and "filer" in saved
        finally:
            window.close()


def test_entry_wiring_strings():
    # main.py 本体は VM では読み込めないので、登録の文字列で確かめる
    # (test_tomorrow_memo と同じ流儀)。
    main_text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '"script-runner"' in main_text
    menu_text = (ROOT / "feature_screen.py").read_text(encoding="utf-8")
    assert "スクリプト実行" in menu_text
    assert "start_script_runner" in menu_text
    send_text = (ROOT / "traytools_send.py").read_text(encoding="utf-8")
    assert "script-runner" in send_text
    filer_text = (ROOT / "filer.py").read_text(encoding="utf-8")
    assert "def cmd_script_runner" in filer_text
    core_text = (ROOT / "filer_core.py").read_text(encoding="utf-8")
    assert '"script_runner"' in core_text
    launch_text = (ROOT / "script_runner_launch.py").read_text(
        encoding="utf-8")
    assert "traytools.script_runner" in launch_text
    settings_text = (ROOT / "settings.py").read_text(encoding="utf-8")
    assert '"script_runner"' in settings_text


if __name__ == "__main__":
    tests = [
        test_window_lists_and_checks,
        test_f5_gating_and_run_copies_result,
        test_error_copy_text,
        test_overwrite_rechecks_and_clears_ran,
        test_run_aborts_when_file_changed_after_check,
        test_window_settings_saved,
        test_entry_wiring_strings,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
