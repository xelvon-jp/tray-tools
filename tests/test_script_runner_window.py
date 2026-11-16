# tests/test_script_runner_window.py
# 「スクリプト実行パネル」の窓(script_runner.py)と入口の配線のテスト。
# チェックの判定そのものは test_script_runner.py が見る。ここでは、窓が
# 一覧を1スクリプト1行の表(ID/ファイル名/状態/時刻/実行ボタン)で
# 作ること、上書きで再チェックになること、F5 の出し分け(問題なしは
# 実行・エラーは実行不可・要確認は F5 では実行しない)、要確認の2段と
# エラーコピーが右クリックのメニューにあること、ダブルクリックで
# EDDy が開くこと、結果がクリップボード文の形で入ること(ID 付きの
# 名前では見出しに ID が入ること)、設定が窓の位置と前面固定を覚える
# こと、起動時に BOM 付け写しの残りが掃除されること、監視フォルダの
# パスが長くても窓幅が設計どおりに収まること、入口(トレイ・外部
# コマンド・あっふ)の配線があること、再開の命令が届くと寝かせ役を
# 置いて通常の閉じ方で閉じることを見る。
#
# 設定は一時ファイル、見張りフォルダも一時フォルダ。実 settings.json には
# 触らない。クリップボードは offscreen で不安定なので、窓の _set_clipboard
# を差し替えて受け取る(窓側の _last_clipboard_text も合わせて立てる)。
# ダークモード(「ダーク」の切替でその場で替わり、起動時から当たること、
# テーマ表のライトの値が従来定数のままであること)も見る。
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

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

import settings as settings_mod  # noqa: E402
import script_runner as sr  # noqa: E402
import script_runner_ai as sr_ai  # noqa: E402
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
    watch.mkdir(exist_ok=True)
    settings_path = tmp / "settings.json"
    data = json.loads(json.dumps(settings_mod.DEFAULT_SETTINGS))
    data["script_runner"] = {
        "watch_folder": str(watch),
        "python": sys.executable,
        "timeout_seconds": 10,
        # 既存のテストは ID の無い名前で行と _items の対応を見るので、
        # 絞り込みは切っておく(絞り込み自体は test_hide_no_id_filter)。
        "hide_no_id": False,
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
            window.table.setCurrentCell(index, 0)
            QCoreApplication.processEvents()
            return item
    raise AssertionError(f"一覧に無い: {name}")


def row_button_texts(window, name: str):
    # 実行の列にあるボタン(行に1つだけ)の文言。
    for index, item in enumerate(window._items):
        if item.name == name:
            widget = window.table.cellWidget(index, sr.COL_RUN)
            if isinstance(widget, QPushButton):
                return [widget.text()]
            return []
    return []


def row_cell_texts(window, name: str):
    # 表の1行分(ID/ファイル名/状態/時刻のセル文言)。
    for index, item in enumerate(window._items):
        if item.name == name:
            return [
                window.table.item(index, col).text()
                for col in (sr.COL_ID, sr.COL_NAME,
                            sr.COL_STATUS, sr.COL_TIME)]
    raise AssertionError(f"一覧に無い: {name}")


def menu_action_texts(window, name: str):
    # 右クリックのメニューに出る項目(区切りは除く)。
    item = item_of(window, name)
    assert item is not None, f"一覧に無い: {name}"
    menu = window._row_menu(item)
    return [a.text() for a in menu.actions() if a.text()]


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
            # 表は1スクリプト1行。アイテムの並びと行が一致する。
            assert window.table.rowCount() == 3
            # 実行ボタンは問題なしと警告の行に、1つ付く(選択と無関係)。
            # 警告も実行できるのは 2026-11-14 の緩和から。
            assert row_button_texts(window, "ok.py") == ["実行"]
            assert row_button_texts(window, "bad.py") == []
            assert row_button_texts(window, "warn.py") == ["実行"]
            # エラーの行: メニューに「エラーをコピー」があり、
            # 実行の項目は無い。「EDDy で開く」は全行共通。
            texts = menu_action_texts(window, "bad.py")
            assert "エラーをコピー" in texts, texts
            assert not any("実行" in t for t in texts), texts
            assert "EDDy で開く" in texts, texts
            # 警告の行: メニューにも実行がある (緩和で2段は無くなった)。
            texts = menu_action_texts(window, "warn.py")
            assert "実行" in texts, texts
            assert "EDDy で開く" in texts, texts
            # 問題なしの行: メニューにも実行がある。
            texts = menu_action_texts(window, "ok.py")
            assert "実行" in texts and "EDDy で開く" in texts, texts
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
            # 警告: F5 でそのまま実行する (2026-11-14 の緩和)。
            select(window, "warn.py")
            window._f5_pressed()
            assert pump_until(lambda: window._running_item is None
                              and copied != [])
            assert copied[-1].startswith("[実行結果] warn.py"), copied[-1]
            assert item_of(window, "warn.py").ran
            copied.clear()
            # 問題なし: F5 で実行し、結果文がクリップボードへ。
            select(window, "ok.py")
            copied.clear()
            window._f5_pressed()
            assert pump_until(lambda: copied != []
                              and window._running_item is None)
            text = copied[-1]
            assert text.startswith("[実行結果] ok.py  終了コード 0"), text
            assert "結果です" in text
            # 末尾には AI への指示文が付く (1.55)。
            assert text.endswith(core.RESULT_AI_PROMPT), text
            assert "クリップボードに入れました" in window.note.text()
            # Ctrl+C で直前の結果をもう一度入れる。
            copied.clear()
            window._copy_last()
            assert copied == [text]
        finally:
            window.close()


def test_phal_status_published_on_run():
    """モニタ表示用の状態 (req-20261115-02): 起動時は待機、実行中は
    run(手数と ID・概要つき)、終われば待機に戻る。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
        os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
        try:
            window, watch, _sp, copied = make_window(tmp)
            try:
                status_path = tmp / "data" / "phal_status.json"

                def read_status():
                    return json.loads(status_path.read_text(
                        encoding="utf-8"))

                # 起動時は待機。版は PHAL_VERSION、手数はまだ 0。
                assert read_status()["state"] == "idle", read_status()
                assert read_status()["version"] == sr.PHAL_VERSION
                assert read_status()["step"] == 0
                write_script(
                    watch, "12-03_sleep.py",
                    "# デモをする\nimport time\ntime.sleep(2)\n"
                    "print('終わり')\n")
                window.show()
                settle_checks(window, ["12-03_sleep.py"])
                select(window, "12-03_sleep.py")
                window._f5_pressed()
                # 実行中は run。手数が 1 になり、ID と概要が入る。
                assert pump_until(
                    lambda: window._running_item is not None
                    and read_status()["state"] == "run"), read_status()
                assert read_status()["step"] == 1
                assert read_status()["command_id"] == "12-03"
                assert read_status()["item"] == "デモをする"
                # 終われば待機に戻る(手数は 1 のまま)。
                assert pump_until(
                    lambda: window._running_item is None
                    and read_status()["state"] == "idle"), read_status()
                assert read_status()["step"] == 1
                assert copied[-1].startswith(
                    "[実行結果] 12-03 12-03_sleep.py  終了コード 0")
            finally:
                window.close()
        finally:
            if prev_data_dir is None:
                os.environ.pop("TRAYTOOLS_DATA_DIR", None)
            else:
                os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_status_strip_shows_step_elapsed_and_item():
    """窓の上の帯 (1.48): 手数・経過時間・今のねらいが1行で出る。
    起動直後は手数 0・「--:--」、実行中は手数 1・経過・ねらい、
    終われば所要時間になる。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
        os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
        try:
            window, watch, _sp, _copied = make_window(tmp)
            try:
                strip = window.status_strip
                # 帯は窓の最上部にあり、1秒ごとに付け直す。
                assert strip is not None
                assert window._strip_timer.isActive()
                assert window._strip_timer.interval() == 1000
                assert "手数 0" in strip.text(), strip.text()
                assert "経過 --:--" in strip.text(), strip.text()
                write_script(
                    watch, "12-03_sleep.py",
                    "# デモをする\nimport time\ntime.sleep(2)\n"
                    "print('終わり')\n")
                window.show()
                settle_checks(window, ["12-03_sleep.py"])
                select(window, "12-03_sleep.py")
                window._f5_pressed()
                # 実行中は手数 1・経過・ねらい。
                assert pump_until(
                    lambda: window._running_item is not None), \
                    strip.text()
                assert "手数 1" in strip.text(), strip.text()
                assert "経過" in strip.text(), strip.text()
                assert "ねらい：デモをする" in strip.text(), strip.text()
                # 終われば所要時間になり、手数とねらいは残る。
                assert pump_until(
                    lambda: window._running_item is None), strip.text()
                assert "手数 1" in strip.text(), strip.text()
                assert "所要" in strip.text(), strip.text()
                assert "ねらい：デモをする" in strip.text(), strip.text()
                # 付け直しの形だけ、決まった値で確かめる。
                window._last_run_seconds = 125.0
                window._refresh_status_strip()
                assert "所要 02:05" in strip.text(), strip.text()
                window._run_started_at = time.monotonic() - 65
                window._refresh_status_strip()
                assert "経過 01:05" in strip.text(), strip.text()
            finally:
                window.close()
        finally:
            if prev_data_dir is None:
                os.environ.pop("TRAYTOOLS_DATA_DIR", None)
            else:
                os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_table_columns_and_double_click_and_id_header():
    import re

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, copied = make_window(tmp)
        opened = []
        original_open = window._open_in_eddy
        window._open_in_eddy = lambda item: opened.append(item.name)
        try:
            write_script(watch, "12-03_demo.py", "print('ID付き')\n")
            write_script(watch, "plain.py", "print('ID無し')\n")
            window.show()
            settle_checks(window, ["12-03_demo.py", "plain.py"])
            # 列の中身: ID はファイル名の先頭から取り、書かれたまま
            # (0 埋めあり)出す。時刻は HH:MM だけ。
            cells = row_cell_texts(window, "12-03_demo.py")
            assert cells[0] == "12-03", cells
            assert cells[1] == "12-03_demo.py", cells
            assert cells[2] == "OK", cells
            assert re.fullmatch(r"\d{2}:\d{2}", cells[3]), cells
            cells = row_cell_texts(window, "plain.py")
            assert cells[0] == "" and cells[2] == "OK", cells
            # ダブルクリックで EDDy で開く(開く先は差し替えて受け取る)。
            row = next(i for i, it in enumerate(window._items)
                       if it.name == "12-03_demo.py")
            window.table.cellDoubleClicked.emit(row, sr.COL_NAME)
            QCoreApplication.processEvents()
            assert opened == ["12-03_demo.py"], opened
            # ID 付きを実行すると、結果の見出しに ID が入り、状態は
            # 判定と合わせて「OK・済」に替わる (1-33)。
            select(window, "12-03_demo.py")
            window._f5_pressed()
            assert pump_until(lambda: copied != []
                              and window._running_item is None)
            text = copied[-1]
            assert text.startswith(
                "[実行結果] 12-03 12-03_demo.py  終了コード 0"), text
            assert row_cell_texts(window, "12-03_demo.py")[2] \
                == "OK・済"
            # ID 無しの結果の見出しは今までどおり。
            copied.clear()
            select(window, "plain.py")
            window._f5_pressed()
            assert pump_until(lambda: copied != []
                              and window._running_item is None)
            assert copied[-1].startswith("[実行結果] plain.py  終了コード 0")
        finally:
            window._open_in_eddy = original_open
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
    # 実行済みハッシュの控えは中身で引くので、他のテストが同じ
    # 中身 (import os / os.remove) を走らせた記録が残っていると、
    # 上書き後の再チェックで実行済みの印が復元されてしまう
    # (1-33)。このテストが見たいのは「同じ項目の」印の外れ方
    # なので、控えの置き場を分ける (1-29 で自動実行のテストに
    # 足したのと同じ作法)。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    os.environ["TRAYTOOLS_DATA_DIR"] = tempfile.mkdtemp(
        prefix="phal-test-data-")
    try:
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
                    lambda: item_of(window, "chg.py").result
                    is not None
                    and item_of(window, "chg.py").result.sha256
                    != old_sha)
                item = item_of(window, "chg.py")
                assert item.result.status == core.STATUS_WARN
                assert not item.ran
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


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
            # 幅は、最小幅(フォントなど環境で変わり、実機では349ほど)より
            # 大きい360にして、保存と復元が一致することを確かめる。
            window.resize(360, 555)
            window.on_top_box.setChecked(True)
            frame = window.frameGeometry()
            window.close()
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            section = saved["script_runner"]
            # 保存値は枠 (フレーム) の座標・大きさ (1.68)。
            assert section["window_geometry"] == [
                frame.x(), frame.y(), frame.width(),
                frame.height()], section
            assert section["always_on_top"] is True
            # 他の節は壊れていない。
            assert "editor" in saved and "filer" in saved
        finally:
            window.close()


def test_window_startup_cleans_bom_leftovers():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        watch = tmp / "watch"
        watch.mkdir()
        leftover = watch / (core.BOM_COPY_PREFIX + "old.ps1")
        leftover.write_text("Write-Output 1\n", encoding="utf-8")
        window, _watch, _sp, _copied = make_window(tmp)
        try:
            # 起動時に、前回残った BOM 付け写しが掃除される(低2)。
            assert not leftover.exists()
        finally:
            window.close()


def test_window_width_with_long_folder_path():
    # 監視フォルダのパスが長いと、見出しラベルが窓の最小幅を押し
    # 広げて設計幅に縮められなかった(低3)。最小幅そのものはフォント
    # など環境で変わるので、短いパスの窓と比べて、長いパスでも最小幅
    # が増えないことと、仕様の上限(360)に収まることを確かめる。
    # フルパスはツールチップで見られること。
    def build(tmp, watch, name):
        settings_path = tmp / name
        data = json.loads(json.dumps(settings_mod.DEFAULT_SETTINGS))
        data["script_runner"] = {
            "watch_folder": str(watch),
            "python": sys.executable,
            "timeout_seconds": 10,
        }
        settings_path.write_text(json.dumps(data, ensure_ascii=False),
                                 encoding="utf-8")
        w = sr.ScriptRunnerWindow(str(settings_path))
        w.show()
        QCoreApplication.processEvents()
        return w

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        short = tmp / "watch"
        short.mkdir()
        base = build(tmp, short, "settings_short.json")
        try:
            base_min = base.minimumWidth()
        finally:
            base.close()
        watch = tmp / ("a" * 40 + "b" * 40) / ("c" * 40 + "d" * 40) \
            / "watch"
        watch.mkdir(parents=True)
        window = build(tmp, watch, "settings_long.json")
        try:
            assert window.minimumWidth() <= base_min + 2, \
                (base_min, window.minimumWidth())
            assert window.minimumWidth() <= 360, window.minimumWidth()
            window.resize(360, 560)
            QCoreApplication.processEvents()
            assert window.width() == 360, window.width()
            assert str(watch) in window.folder_caption.toolTip()
        finally:
            window.close()


class _FakeSocket:
    """CommandServer._read に渡す、読み取りだけの偽の受け口。"""

    def __init__(self, data: bytes):
        self._data = data

    def readAll(self):
        return self._data


def test_restart_command_arranges_helper_and_closes():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, _watch, settings_path, _copied = make_window(tmp)
        arranged = []
        original = sr.script_runner_launch.spawn_respawn_helper

        def fake_helper(path=None):
            arranged.append(path)
            return 777

        sr.script_runner_launch.spawn_respawn_helper = fake_helper
        try:
            window.show()
            QCoreApplication.processEvents()
            server = sr.CommandServer(window)
            server._read(_FakeSocket(b"restart\n"))
            QCoreApplication.processEvents()
            # 寝かせ役が(設定パス付きで)置かれ、窓は通常の閉じ方で閉じた。
            assert arranged == [str(settings_path)], arranged
            assert not window.isVisible()
            # 閉じたときの設定保存が走っていること。
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            assert "window_geometry" in saved["script_runner"]
        finally:
            sr.script_runner_launch.spawn_respawn_helper = original
            window.close()


def test_entry_wiring_strings():
    # main.py 本体は VM では読み込めないので、登録の文字列で確かめる
    # (test_tomorrow_memo と同じ流儀)。
    main_text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '"script-runner"' in main_text
    assert '"script-runner-restart"' in main_text
    assert "_script_runner_restart_command" in main_text
    menu_text = (ROOT / "feature_screen.py").read_text(encoding="utf-8")
    assert "スクリプト実行" in menu_text
    assert "start_script_runner" in menu_text
    assert "restart_script_runner" in menu_text
    send_text = (ROOT / "traytools_send.py").read_text(encoding="utf-8")
    assert "script-runner" in send_text
    assert "script-runner-restart" in send_text
    filer_text = (ROOT / "filer.py").read_text(encoding="utf-8")
    assert "def cmd_script_runner" in filer_text
    core_text = (ROOT / "filer_core.py").read_text(encoding="utf-8")
    assert '"script_runner"' in core_text
    launch_text = (ROOT / "script_runner_launch.py").read_text(
        encoding="utf-8")
    assert "traytools.script_runner" in launch_text
    assert "RESTART_COMMAND" in launch_text
    assert "respawn_when_gone" in launch_text
    settings_text = (ROOT / "settings.py").read_text(encoding="utf-8")
    assert '"script_runner"' in settings_text


def test_hide_no_id_filter():
    # 絞り込みの既定は ON(設定にキーが無い)。ID のある行だけが出て、
    # 隠れた方も裏でチェックは済む。OFF で両方出て、設定が残り、
    # 次の窓で OFF のまま戻ることまでを一気に見る。
    def row_names(window):
        return [window.table.item(r, sr.COL_NAME).text()
                for r in range(window.table.rowCount())]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, settings_path, _copied = make_window(tmp)
        window.close()
        # make_window は絞り込みを切ってあるので、節からキーを抜いて
        # 「キーが無い = 既定 ON」の窓を作り直す。
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        data["script_runner"].pop("hide_no_id", None)
        settings_path.write_text(json.dumps(data, ensure_ascii=False),
                                 encoding="utf-8")
        window = sr.ScriptRunnerWindow(str(settings_path))
        try:
            write_script(watch, "7-1_a.py", "print('a')\n")
            write_script(watch, "plain.py", "print('p')\n")
            window.show()
            settle_checks(window, ["7-1_a.py", "plain.py"])
            assert window.hide_no_id_box.isChecked()
            assert row_names(window) == ["7-1_a.py"], row_names(window)
            # 隠れた方も、裏でチェックは済んでいる。
            assert item_of(window, "plain.py").result is not None
            window.hide_no_id_box.setChecked(False)
            assert sorted(row_names(window)) == ["7-1_a.py", "plain.py"]
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            assert saved["script_runner"]["hide_no_id"] is False
        finally:
            window.close()
        # 次の窓では OFF のまま戻る。
        window = sr.ScriptRunnerWindow(str(settings_path))
        try:
            window.show()
            settle_checks(window, ["7-1_a.py", "plain.py"])
            assert not window.hide_no_id_box.isChecked()
            assert sorted(row_names(window)) == ["7-1_a.py", "plain.py"]
        finally:
            window.close()


def test_return_and_trash_actions():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, copied = make_window(tmp)
        try:
            # フォーカス枠を消す delegate が入っていること。
            assert isinstance(window.table.itemDelegate(),
                              sr._NoFocusDelegate)
            # チェックは通るが実行時にエラーになるスクリプト。
            write_script(watch, "5-2_fix.py",
                         "import sys\n"
                         "sys.stderr.write('boom\\n')\n"
                         "sys.exit(3)\n")
            window.show()
            settle_checks(window, ["5-2_fix.py"])
            item = select(window, "5-2_fix.py")
            texts = menu_action_texts(window, "5-2_fix.py")
            assert "差し戻す…" in texts and "ゴミ箱に移す" in texts, texts
            # 実行して失敗させる。結果は項目にも残る。
            window._f5_pressed()
            assert pump_until(lambda: copied != []
                              and window._running_item is None)
            assert "終了コード 3" in copied[-1]
            assert item.last_run is not None
            assert item.last_run.exit_code == 3
            # 差し戻す: 理由欄は差し替えて、文がクリップボードへ入る。
            original = sr.QInputDialog.getMultiLineText
            sr.QInputDialog.getMultiLineText = staticmethod(
                lambda *a, **k: ("直っていません", True))
            try:
                window._return_item(item)
            finally:
                sr.QInputDialog.getMultiLineText = original
            text = copied[-1]
            assert text.startswith("[差し戻し] 5-2 5-2_fix.py"), text
            assert "理由: 直っていません" in text
            assert "終了コード 3" in text and "boom" in text
            assert "--- スクリプト ---" in text and "sys.exit(3)" in text
            assert item.returned
            assert window._status_cell_text(item) == "差し戻し済"
            assert "差し戻しの文をクリップボード" in window.note.text()
            # ゴミ箱: 失敗(VM では移せない)なら失敗の知らせが出て、
            # ファイルは残る。成功の差し替えでは成功の知らせになる。
            window._trash_item(item)
            assert "ゴミ箱に移せませんでした" in window.note.text()
            assert (watch / "5-2_fix.py").exists()
            moved = []
            real = core.move_to_recycle_bin
            core.move_to_recycle_bin = (
                lambda p: moved.append(Path(p)) or True)
            try:
                window._trash_item(item)
            finally:
                core.move_to_recycle_bin = real
            assert moved == [item.path]
            assert "ゴミ箱に移しました" in window.note.text()
        finally:
            window.close()


def test_run_button_press_and_reuse():
    # 行の実行ボタン: 実際にマウスで押すと実行が始まり、結果文が
    # クリップボードに入ること(F5 と同じ経路になること)。警告の行も
    # 同じ。姿が変わらないボタンは、一覧の作り直しでも使い回される
    # こと(作り直しでクリックが消えない)。タイトルと見出しも見る。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, copied = make_window(tmp)
        try:
            write_script(watch, "ok.py", "print('結果です')\n")
            write_script(watch, "warn.py", "import os\nos.remove('x')\n")
            window.show()
            settle_checks(window, ["ok.py", "warn.py"])
            assert window.windowTitle() == (
                "擬似エージェントループ支援ツール PHAL Ver"
                + sr.PHAL_VERSION), window.windowTitle()
            headings = [label.text()
                        for label in window.findChildren(QLabel)]
            assert "PHAL：PHAL Helps Agent Loop" in headings, headings
            for name in ("ok.py", "warn.py"):
                copied.clear()
                item = select(window, name)
                row = window.table.currentRow()
                button = window.table.cellWidget(row, sr.COL_RUN)
                assert isinstance(button, QPushButton), name
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                assert pump_until(lambda: window._running_item is None
                                  and copied != []), name
                assert copied[-1].startswith(f"[実行結果] {name}"), \
                    copied[-1]
                assert item.ran, name
            # 姿が変わらないボタンは、作り直しでも同じ物が残る。
            select(window, "ok.py")
            row = window.table.currentRow()
            before = window.table.cellWidget(row, sr.COL_RUN)
            assert isinstance(before, QPushButton)
            window._refresh_list()
            assert window.table.cellWidget(row, sr.COL_RUN) is before
        finally:
            window.close()


def test_poll_problem_reported_and_others_listed():
    # 見回り: 1件のファイルで例外が出ても、他のファイルは一覧に出て、
    # 問題は note に1度だけ出る(黙って一覧が止まらない)。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, _copied = make_window(tmp)
        original = core.extract_summary

        def boom(path):
            if str(path).endswith("bad.py"):
                raise RuntimeError("わざとの失敗")
            return original(path)

        core.extract_summary = boom
        try:
            window.show()
            write_script(watch, "bad.py", "print('bad')\n")
            write_script(watch, "good.py", "print('good')\n")
            assert pump_until(
                lambda: item_of(window, "good.py") is not None)
            assert item_of(window, "bad.py") is None
            assert window.table.rowCount() == 1
            assert "bad.py" in window.note.text(), window.note.text()
            # 何度見回っても、同じ問題の通知は増えない。
            window._poll()
            window._poll()
            assert len(window._poll_problem_seen) == 1
        finally:
            core.extract_summary = original
            window.close()


def _make_auto_window(tmp: Path, auto_run: bool):
    """自動実行の切替を指定して窓を作る下ごしらえ。

    監視フォルダへ先にファイルを置いてから窓を construct したい
    (起動前からあった分の再現) ので、make_window とは分けた。
    戻り値は (窓, 監視フォルダ, コピーされた文の控え)。"""
    watch = tmp / "watch"
    watch.mkdir(exist_ok=True)
    settings_path = tmp / "settings.json"
    data = json.loads(json.dumps(settings_mod.DEFAULT_SETTINGS))
    section = {
        "watch_folder": str(watch),
        "python": sys.executable,
        "timeout_seconds": 10,
        "hide_no_id": False,
    }
    if auto_run:
        section["auto_run"] = True
    data["script_runner"] = section
    settings_path.write_text(json.dumps(data, ensure_ascii=False),
                             encoding="utf-8")
    return settings_path, watch


def test_auto_run_only_after_launch_files():
    # 自動実行 (auto_run): 起動前からあったファイルは決して自動で
    # 走らないこと、起動後に現れたファイルはチェックが問題なし・
    # 警告なら自動で (直列に) 走ること、文法エラーは走らないこと、
    # 切替が切なら起動後の分も走らないことを見る。実行そのものは
    # 控えの関数に差し替えて、実プロセスは起こさない。
    # 1.63 の対象範囲 (案3) で、自動実行の対象は「自動で落とした
    # ファイル」か「ID つきのファイル」だけになったため、ここは
    # ID つきの名前で見る (範囲そのものは
    # test_auto_run_scope_plan3 が見る)。
    # 控えの置き場は一時フォルダへ向ける (1-29 の実行済みハッシュ
    # の控えは中身で引くので、他のテストが同じ中身を走らせた記録
    # が残っていると、このテストの警告ファイルまで止まってしまう)。
    calls: list[str] = []
    active = {"now": 0, "peak": 0}
    original_execute = core.execute_script
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def fake_execute(path, config, handle=None, expected_sha256=None):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        try:
            time.sleep(0.25)
            calls.append(Path(path).name)
            return core.RunResult(
                name=Path(path).name, path=str(path), exit_code=0,
                seconds=0.25, stdout="done\n",
                sha256=expected_sha256 or "")
        finally:
            active["now"] -= 1

    core.execute_script = fake_execute
    try:
        # --- 切替 入: 起動前の分は走らず、起動後の分だけ走る ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_auto_window(tmp, True)
            write_script(watch, "backlog.py", "print('古い分')\n")
            window = sr.ScriptRunnerWindow(str(settings_path))
            copied: list[str] = []

            def fake_clipboard(text, _w=window, _c=copied):
                _c.append(text)
                _w._last_clipboard_text = text

            window._set_clipboard = fake_clipboard  # type: ignore
            try:
                window._poll()  # 起動前の分が一覧に載る
                settle_checks(window, ["backlog.py"])
                # チェックが済んでも、起動前からあった分は走らない。
                assert pump_until(lambda: False, timeout=0.6) is False
                window._poll()
                assert calls == [], calls
                backlog = item_of(window, "backlog.py")
                assert backlog is not None and not backlog.ran
                assert backlog.last_run is None
                assert backlog.auto_eligible is False
                # 起動後に現れた分: 問題なし・警告は走り、エラーは
                # 走らない。2件は直列 (同時に走らない)。
                write_script(watch, "1-01_clean.py", "print('新しい分')\n")
                write_script(watch, "1-02_warn.py",
                             "import os\nos.remove('x')\n")
                write_script(watch, "1-03_broken.py", "def broken(:\n")
                window._poll()
                window._poll()
                settle_checks(window,
                              ["1-01_clean.py", "1-02_warn.py", "1-03_broken.py"])
                assert pump_until(lambda: len(calls) == 2), calls
                assert sorted(calls) == ["1-01_clean.py", "1-02_warn.py"], calls
                assert active["peak"] == 1, active
                broken = item_of(window, "1-03_broken.py")
                assert broken is not None and not broken.ran
                assert broken.last_run is None
                clean = item_of(window, "1-01_clean.py")
                assert clean is not None and clean.ran
                # 同じ中身は2度走らない (見回りが回っても)。
                window._poll()
                assert pump_until(lambda: False, timeout=0.6) is False
                assert len(calls) == 2, calls
                assert not backlog.ran  # 念のため: 古い分は最後まで
            finally:
                window.close()
        # --- 切替 切: 起動後の分も自動では走らない ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_auto_window(tmp, False)
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            try:
                write_script(watch, "1-04_late.py", "print('切替なし')\n")
                window._poll()
                window._poll()
                settle_checks(window, ["1-04_late.py"])
                assert pump_until(lambda: False, timeout=0.6) is False
                late = item_of(window, "1-04_late.py")
                assert late is not None and not late.ran
                assert len(calls) == 2, calls  # 増えていない
            finally:
                window.close()
    finally:
        core.execute_script = original_execute
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_loop_survives_restart():
    # 再開をまたぐループ (1-28):
    # (a) 起動後に現れたファイルは、対象の控えが残るので、再開
    #     (新しい窓) の後に起動時の滞留の姿でも対象のままで、
    #     自動で走ること。(1.63 の対象範囲 (案3) に合わせ、
    #     fresh は ID つきの名前にしてある。)
    # (b) 控えの無い滞留分は、再開後も自動では走らないこと。
    # (c) 連鎖の途中 (回答の投入中を装う) で届いた開き直しの
    #     頼みは、連鎖が済むまで実行されず、節目で実行される
    #     こと。何も動いていなければ即座に実行されること。
    # (d) 走ったファイルの控えは消えること。
    # 控えの置き場は TRAYTOOLS_DATA_DIR で一時フォルダへ向ける。
    # 実行そのものは控えの関数に差し替えて、実プロセスは起こさない。
    calls: list[str] = []
    original_execute = core.execute_script

    def fake_execute(path, config, handle=None, expected_sha256=None):
        calls.append(Path(path).name)
        return core.RunResult(
            name=Path(path).name, path=str(path), exit_code=0,
            seconds=0.0, stdout="done\n",
            sha256=expected_sha256 or "")

    core.execute_script = fake_execute
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            # --- 窓 A: 切替は切。起動前の分は対象にならず、起動後の
            # 分は対象になって控えが残る (切替が切でも走らない)。 ---
            settings_path, watch = _make_auto_window(tmp, False)
            write_script(watch, "backlog.py", "print('古い分')\n")
            window_a = sr.ScriptRunnerWindow(str(settings_path))
            window_a._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window_a, "_last_clipboard_text",
                                     text))
            try:
                window_a._poll()
                settle_checks(window_a, ["backlog.py"])
                backlog_a = item_of(window_a, "backlog.py")
                assert backlog_a is not None
                assert backlog_a.auto_eligible is False
                write_script(watch, "1-01_fresh.py", "print('新しい分')\n")
                window_a._poll()
                window_a._poll()
                settle_checks(window_a, ["1-01_fresh.py"])
                fresh_a = item_of(window_a, "1-01_fresh.py")
                assert fresh_a is not None
                assert fresh_a.auto_eligible is True
                assert not fresh_a.ran and calls == [], calls
                records = core.load_pending_runs()
                assert str(watch / "1-01_fresh.py") in records, records
                assert str(watch / "backlog.py") not in records, records
            finally:
                window_a.close()
            # --- 窓 B (再開後): 切替を入れる。控えのある fresh は
            # 滞留の姿でも対象のままで走り、控えの無い backlog は
            # 走らない。走った fresh の控えは消える。 ---
            settings_path, watch = _make_auto_window(tmp, True)
            window_b = sr.ScriptRunnerWindow(str(settings_path))
            window_b._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window_b, "_last_clipboard_text",
                                     text))
            try:
                window_b._poll()
                settle_checks(window_b, ["backlog.py", "1-01_fresh.py"])
                assert pump_until(lambda: calls == ["1-01_fresh.py"]), calls
                assert pump_until(lambda: False, timeout=0.6) is False
                assert calls == ["1-01_fresh.py"], calls
                backlog_b = item_of(window_b, "backlog.py")
                assert backlog_b is not None and not backlog_b.ran
                assert backlog_b.auto_eligible is False
                fresh_b = item_of(window_b, "1-01_fresh.py")
                assert fresh_b is not None and fresh_b.ran
                assert fresh_b.auto_eligible is True
                records = core.load_pending_runs()
                assert str(watch / "1-01_fresh.py") not in records, records
            finally:
                window_b.close()
        # --- (c) 開き直しの保留: 連鎖の途中では待たせ、節目で開く ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_auto_window(tmp, False)
            arranged = []
            original_helper = sr.script_runner_launch \
                .spawn_respawn_helper

            def fake_helper(path=None):
                arranged.append(path)
                return 777

            sr.script_runner_launch.spawn_respawn_helper = fake_helper
            try:
                window = sr.ScriptRunnerWindow(str(settings_path))
                try:
                    window.show()
                    QCoreApplication.processEvents()
                    # 回答の投入中を装う。その間の頼みは保留される。
                    window._ai_filling = True
                    window.request_restart()
                    assert arranged == [], arranged
                    assert window._restart_deferred is True
                    assert window.isVisible()
                    # 連鎖の節目 (回答の終わり) で保留が実行される。
                    window._ai_filling = False
                    window._on_ai_answer_done(
                        sr._AiAnswerResult(True, "ok"))
                    QCoreApplication.processEvents()
                    assert arranged == [str(settings_path)], arranged
                    assert not window.isVisible()
                finally:
                    window.close()
                # 何も動いていなければ、その場で開き直す。
                window = sr.ScriptRunnerWindow(str(settings_path))
                try:
                    window.show()
                    QCoreApplication.processEvents()
                    window.request_restart()
                    assert arranged == [str(settings_path),
                                        str(settings_path)], arranged
                    assert not window.isVisible()
                finally:
                    window.close()
            finally:
                sr.script_runner_launch.spawn_respawn_helper = \
                    original_helper
    finally:
        core.execute_script = original_execute
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def _make_dl_window(tmp: Path, **section_extra):
    """DL まわりの切替や投げ先を指定して窓を作る下ごしらえ。

    戻り値は (設定ファイル, 監視フォルダ)。窓そのものは呼び出し側
    が作る (ファイルを先に置いてから construct したい場合がある
    ため)。"""
    watch = tmp / "watch"
    watch.mkdir(exist_ok=True)
    settings_path = tmp / "settings.json"
    data = json.loads(json.dumps(settings_mod.DEFAULT_SETTINGS))
    section = {
        "watch_folder": str(watch),
        "python": sys.executable,
        "timeout_seconds": 10,
        "hide_no_id": False,
    }
    section.update(section_extra)
    data["script_runner"] = section
    settings_path.write_text(json.dumps(data, ensure_ascii=False),
                             encoding="utf-8")
    return settings_path, watch


def test_dl_loop_brakes():
    # DL 無限ループの歯止め (1-29):
    # (a) 候補の概要が一覧のファイルの概要と一致すれば、DL の
    #     特別行は出ず、自動でも押さないこと。一致しない候補は
    #     従来どおり出て、自動で押せること。
    # (b) 押した候補の控えは開き直しをまたいで残り、落とした
    #     ファイルがある間は再び出ないこと。ファイルが消えたら
    #     再び出ること。
    # (c) 実行済みの中身 (sha256) の写しが起動後に届いても自動
    #     では走らず、知らせが1度出ること。別の中身は自動で
    #     走ること。写しの手動実行は妨げないこと。
    #     (1.63 の対象範囲 (案3) に合わせ、(c) のファイルは ID
    #     つきの名前にしてある。範囲外のファイルは自動では
    #     走らないので、歯止めの判定に届かないため。)
    # 控えの置き場は TRAYTOOLS_DATA_DIR で一時フォルダへ向ける。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        # --- (a) 概要の一致で DL を止める ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, auto_dl=True,
                ai_target={"process_name": "dummy.exe"})
            write_script(
                watch, "9-1_install_phal.phal",
                "#!phal python\n"
                "# 9-1: DLブレーキのテスト用スクリプト\n"
                "print('中身')\n")
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            presses: list = []
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_presser(profile, key=None, _p=presses):
                _p.append(profile)
                return sr._AiAnswerResult(True, "ok")

            window.ai_dl_presser = fake_presser  # type: ignore
            try:
                window._poll()
                settle_checks(window, ["9-1_install_phal.phal"])
                card = ("#!phal python\n"
                        "# 9-1: DLブレーキのテスト用スクリプト\n"
                        "import base64, hashlib\n")
                candidate = {
                    "name": card, "class_name": "cls group/card",
                    "label": "9-1: DLブレーキのテスト用スクリプト",
                    "summary": "9-1: DLブレーキのテスト用スクリプト",
                }
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate))
                QCoreApplication.processEvents()
                # 概要が一致するので、行は出ず自動でも押さない。
                assert window._watch_list[0].holder is None
                assert presses == [], presses
                # 概要の違う候補は従来どおり出て、自動で押せる。
                other = {
                    "name": "#!phal python\n# 9-9: 別のスクリプト\n"
                            "print(1)\n",
                    "class_name": "cls group/card",
                    "label": "9-9: 別のスクリプト",
                    "summary": "9-9: 別のスクリプト",
                }
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", other))
                assert pump_until(lambda: len(presses) == 1), presses
                assert pump_until(
                    lambda: window._watch_list[0].holder is None
                    and not window._dl_pressing)
            finally:
                window.close()
        # --- (b) 押下済みの控えが開き直しをまたぐ ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            candidate = {
                "name": "#!phal python\n"
                        "# 9-2: 控えのテスト用スクリプトです\n"
                        "import base64\n",
                "class_name": "cls group/card",
                "label": "9-2: 控えのテスト用スクリプトです",
                "summary": "9-2: 控えのテスト用スクリプトです",
            }
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))
            window.ai_dl_presser = (  # type: ignore[method-assign]
                lambda profile, key=None: sr._AiAnswerResult(True, "ok"))
            try:
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate))
                assert window._watch_list[0].holder is not None
                window._press_download(auto=False)
                assert pump_until(
                    lambda: not window._dl_pressing
                    and window._watch_list[0].holder is None)
                # 押下を始めた時点で、控えがファイルに残っている。
                records = core.load_dl_consumed()
                key = core.dl_consumed_key(
                    candidate["name"], candidate["class_name"])
                assert key in records, records
            finally:
                window.close()
            # 落としたファイルが届いてから、開き直す。
            write_script(
                watch, "9-2_install_phal.phal",
                "#!phal python\n"
                "# 9-2: 控えのテスト用スクリプトです\n"
                "print('中身')\n")
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))
            try:
                window._poll()
                settle_checks(window, ["9-2_install_phal.phal"])
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate))
                QCoreApplication.processEvents()
                # 控え + ファイル有り → 再び出ない。
                assert window._watch_list[0].holder is None
                # ファイルが消えたら、落とし直せるよう再び出る。
                (watch / "9-2_install_phal.phal").unlink()
                window._poll()
                assert pump_until(
                    lambda: item_of(window,
                                    "9-2_install_phal.phal") is None)
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate))
                QCoreApplication.processEvents()
                assert window._watch_list[0].holder is not None
            finally:
                window.close()
        # --- (c) 実行済みの中身は自動で走らせない ---
        calls: list[str] = []
        original_execute = core.execute_script

        def fake_execute(path, config, handle=None,
                         expected_sha256=None):
            calls.append(Path(path).name)
            return core.RunResult(
                name=Path(path).name, path=str(path), exit_code=0,
                seconds=0.0, stdout="done\n",
                sha256=expected_sha256 or "")

        core.execute_script = fake_execute
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
                settings_path, watch = _make_auto_window(tmp, True)
                window = sr.ScriptRunnerWindow(str(settings_path))
                window._set_clipboard = (  # type: ignore[method-assign]
                    lambda text: setattr(
                        window, "_last_clipboard_text", text))
                try:
                    window._poll()  # 起動スナップショット (空)
                    content = "print('同じ中身です')\n"
                    write_script(watch, "1-01_orig.py", content)
                    window._poll()
                    window._poll()
                    settle_checks(window, ["1-01_orig.py"])
                    assert pump_until(
                        lambda: calls == ["1-01_orig.py"]), calls
                    assert pump_until(
                        lambda: window._running_item is None)
                    sha = core.file_sha256(watch / "1-01_orig.py")
                    assert core.executed_hash_contains(
                        core.load_executed_hashes(), sha)
                    # 同じ中身の写しが届いても、自動では走らない。
                    write_script(watch, "1-01_orig (2).py", content)
                    window._poll()
                    window._poll()
                    settle_checks(window, ["1-01_orig (2).py"])
                    assert pump_until(
                        lambda: False, timeout=0.6) is False
                    assert calls == ["1-01_orig.py"], calls
                    dup = item_of(window, "1-01_orig (2).py")
                    # 中身が実行済みなので、1-33 からは実行済みの
                    # 印が復元されて付く (状態は「OK・済」になる)。
                    # ただし写し自身はこの会話では走っていない
                    # (last_run は無い) し、自動でも走らない。
                    assert dup is not None and dup.ran
                    assert dup.last_run is None
                    assert dup.auto_skip_executed is True
                    assert "同じ内容を実行済みのため" \
                        in window.note.text(), window.note.text()
                    # 別の中身は自動で走る。
                    write_script(watch, "1-02_other.py",
                                 "print('別の中身です')\n")
                    window._poll()
                    window._poll()
                    settle_checks(window, ["1-02_other.py"])
                    assert pump_until(
                        lambda: "1-02_other.py" in calls), calls
                    assert pump_until(
                        lambda: window._running_item is None)
                    # 写しの手動実行は妨げない。
                    window._start_run(dup)
                    assert pump_until(
                        lambda: "1-01_orig (2).py" in calls), calls
                finally:
                    window.close()
        finally:
            core.execute_script = original_execute
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dl_launch_candidate_not_auto_downloaded():
    # 起動時に既にあった DL 候補は自動ダウンロードの対象外 (1.54):
    # (a) 起動直後の最初の検出で見えた候補は、特別行は出るが
    #     自動では押さない。手動では押せること。
    # (b) その後に届いた新しい候補は、従来どおり自動で押すこと。
    # (c) 起動時の最初の検出で候補が無ければ、その後に届いた
    #     候補は自動で押すこと (空のスナップショット)。
    # 狙い: 設置の開き直しの直後に、古い添付を拾い直して
    # 落とし直す (表に「(1)」付きの重複行ができる) のを防ぐ。
    # 控えの置き場は TRAYTOOLS_DATA_DIR で一時フォルダへ向ける。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        candidate_a = {
            "name": "#!phal python\n# 9-3: 起動時にあった候補です\n"
                    "print(1)\n",
            "class_name": "cls group/card",
            "label": "9-3: 起動時にあった候補です",
            "summary": "9-3: 起動時にあった候補です",
        }
        candidate_b = {
            "name": "#!phal python\n# 9-4: 起動後に届いた候補です\n"
                    "print(2)\n",
            "class_name": "cls group/card",
            "label": "9-4: 起動後に届いた候補です",
            "summary": "9-4: 起動後に届いた候補です",
        }
        # --- (a)(b) 起動時に候補があった窓 ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_dl=True,
                ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            presses: list = []
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_presser(profile, key=None, _p=presses):
                _p.append(profile)
                return sr._AiAnswerResult(True, "ok")

            window.ai_dl_presser = fake_presser  # type: ignore
            try:
                # (a) 起動直後の最初の検出。行は出るが自動では
                # 押さない。
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate_a))
                QCoreApplication.processEvents()
                assert window._watch_list[0].holder is not None
                assert window._watch_list[0].holder.key \
                    in window._watch_list[0].launch_keys
                assert presses == [], presses
                # 手動では押せる。
                window._press_download(auto=False)
                assert pump_until(
                    lambda: len(presses) == 1
                    and window._watch_list[0].holder is None
                    and not window._dl_pressing), presses
                # (b) その後に届いた新しい候補は自動で押す。
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate_b))
                assert pump_until(
                    lambda: len(presses) == 2
                    and window._watch_list[0].holder is None
                    and not window._dl_pressing), presses
            finally:
                window.close()
        # --- (c) 起動時に候補が無かった窓 ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_dl=True,
                ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            # presses は (a)(b) と同じリストのまま使う
            # (fake_presser が掴んでいるリストと同一にするため)。
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))
            window.ai_dl_presser = fake_presser  # type: ignore
            try:
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None))
                QCoreApplication.processEvents()
                assert window._watch_list[0].launch_keys == set()
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate_b))
                assert pump_until(
                    lambda: len(presses) == 3
                    and window._watch_list[0].holder is None
                    and not window._dl_pressing), presses
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dl_multiple_candidates_pick_unconsumed():
    # 候補が複数あるときの選択と形跡 (1-57):
    # (a) 末尾のカードが消費済みでも、その手前の未消費のカード
    #     が特別行に出ること (従来は末尾の1件に固定で、本命が
    #     浮上しなかった。1-76 の最有力の原因)。押下役には、
    #     選んだ候補の key (名前+クラス名) が渡ること。
    # (b) 全部が抑制されたら行は出ず、診断に「直近の走査」の
    #     1行 (抑制の理由つき) が残り、束にも入ること。検出失敗
    #     も同じ形で残り、同じ失敗が続く間は1回だけになること。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        honmei = {
            "name": "#!phal python\n# 9-7: 本命のスクリプトです\n"
                    "print(1)\n",
            "class_name": "cls group/card",
            "label": "9-7: 本命のスクリプトです",
            "summary": "9-7: 本命のスクリプトです",
        }
        tail = {
            "name": "#!phal python\n# 9-8: 末尾のスクリプトです\n"
                    "print(2)\n",
            "class_name": "cls group/card",
            "label": "9-8: 末尾のスクリプトです",
            "summary": "9-8: 末尾のスクリプトです",
        }
        # --- (a) 末尾が消費済みでも本命が出る ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))
            presses: list = []
            window.ai_dl_presser = (  # type: ignore[method-assign]
                lambda profile, key=None: presses.append(key)
                or sr._AiAnswerResult(True, "ok"))
            try:
                # まず末尾のカードだけが見える。押して消費済みに
                # する。
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", tail))
                QCoreApplication.processEvents()
                assert window._watch_list[0].holder is not None
                window._press_download(auto=False)
                assert pump_until(
                    lambda: len(presses) == 1
                    and window._watch_list[0].holder is None
                    and not window._dl_pressing)
                # 押下役には、選んだ候補の key が渡っている。
                assert presses[0] == {
                    "name": tail["name"],
                    "class_name": tail["class_name"]}, presses
                # 本命と末尾の両方が見える走査。末尾は消費済み
                # なので、本命が特別行に出る。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", tail, candidates=[honmei, tail]))
                QCoreApplication.processEvents()
                assert window._watch_list[0].holder is not None
                assert window._watch_list[0].holder.summary \
                    == honmei["summary"]
                assert any(
                    "直近の走査: 候補あり（9-7: 本命のスクリプトです）"
                    in e["text"] for e in window._diag_events)
            finally:
                window.close()
        # --- (b) 全部抑制と検出失敗の形跡 ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            # 両方の候補とも、概要の一致するファイルが既に
            # 一覧にある状態にする。
            write_script(
                watch, "9-7_install_phal.phal",
                "#!phal python\n# 9-7: 本命のスクリプトです\n"
                "print('中身')\n")
            write_script(
                watch, "9-8_install_phal.phal",
                "#!phal python\n# 9-8: 末尾のスクリプトです\n"
                "print('中身')\n")
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))
            try:
                window._poll()
                settle_checks(
                    window, ["9-7_install_phal.phal",
                             "9-8_install_phal.phal"])
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", tail, candidates=[honmei, tail]))
                QCoreApplication.processEvents()
                assert window._watch_list[0].holder is None
                scans = [e["text"] for e in window._diag_events
                         if e["text"].startswith("直近の走査:")]
                assert scans and "候補なし（抑制2件:" in scans[-1], \
                    scans
                assert "一覧に既にある" in scans[-1], scans
                bundle = window._diagnostics_text()
                assert "- 直近の走査: 候補なし" in bundle, bundle
                # 検出失敗も残る。同じ失敗が続く間は1回だけ。
                window._on_dl_checked(sr._DlCheckResult(
                    False, "窓が見つかりません", None))
                window._on_dl_checked(sr._DlCheckResult(
                    False, "窓が見つかりません", None))
                fails = [
                    e["text"] for e in window._diag_events
                    if "検出失敗（窓が見つかりません）" in e["text"]]
                assert len(fails) == 1, fails
                assert fails[0].startswith("直近の走査: 検出失敗")
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_status_verdict_and_ran_survives_restart():
    # 状態列の2軸表示と、実行済みの印の復元 (1-33):
    # (a) 警告のファイルを実行すると、状態が「警告・済」になる
    #     (判定が「済」に隠れない) こと。色は判定 (警告) のまま。
    # (b) 開き直した (新しい窓) 後も、チェックの控えと実行済み
    #     ハッシュの控えから「警告・済」に戻ること。その際
    #     last_run は無いが、前回の結果の控え (1.56) があるので
    #     行のボタンは「再実行」になり、自動実行 (切替 入) でも
    #     走らないこと。
    # (c) 実行していない問題なしのファイルは「OK」のまま。
    # (d) 中身を書き換えると再チェックで「・済」が取れること。
    # 控えの置き場は TRAYTOOLS_DATA_DIR で一時フォルダへ向ける
    # (実行済みハッシュは中身で引くので、他と干渉させない)。
    # 実行そのものは控えの関数に差し替えて、実プロセスは起こさない。
    calls: list[str] = []
    original_execute = core.execute_script

    def fake_execute(path, config, handle=None, expected_sha256=None):
        calls.append(Path(path).name)
        return core.RunResult(
            name=Path(path).name, path=str(path), exit_code=0,
            seconds=0.0, stdout="done\n",
            sha256=expected_sha256 or "")

    core.execute_script = fake_execute
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            # --- 窓 A (切替は切): 警告を実行して「警告・済」に ---
            settings_path, watch = _make_auto_window(tmp, False)
            write_script(watch, "warn.py",
                         "import os\nos.remove('x')\n")
            write_script(watch, "ok.py", "print('問題なし')\n")
            window_a = sr.ScriptRunnerWindow(str(settings_path))
            copied_a: list[str] = []

            def fake_clipboard_a(text, _w=window_a, _c=copied_a):
                _c.append(text)
                _w._last_clipboard_text = text

            window_a._set_clipboard = fake_clipboard_a  # type: ignore
            try:
                window_a.show()
                settle_checks(window_a, ["warn.py", "ok.py"])
                # 実行前は判定だけが出る。
                assert row_cell_texts(window_a, "warn.py")[2] == "警告"
                assert row_cell_texts(window_a, "ok.py")[2] == "OK"
                select(window_a, "warn.py")
                window_a._f5_pressed()
                assert pump_until(lambda: copied_a != []
                                  and window_a._running_item is None)
                assert calls == ["warn.py"], calls
                warn_a = item_of(window_a, "warn.py")
                assert warn_a is not None and warn_a.ran
                # (a) 判定と済の両方が出て、色は警告のまま。
                assert row_cell_texts(window_a, "warn.py")[2] \
                    == "警告・済"
                row_a = next(
                    i for i, it in enumerate(window_a._items)
                    if it.name == "warn.py")
                cell = window_a.table.item(row_a, sr.COL_STATUS)
                assert cell.foreground().color().name() \
                    == sr.STATUS_COLORS[core.STATUS_WARN]
            finally:
                window_a.close()
            # --- 窓 B (再開後・切替 入): 控えから「警告・済」に
            # 戻り、ボタンは「実行」、自動では走らない ---
            settings_path, watch = _make_auto_window(tmp, True)
            window_b = sr.ScriptRunnerWindow(str(settings_path))
            window_b._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window_b, "_last_clipboard_text",
                                     text))
            try:
                window_b._poll()
                settle_checks(window_b, ["warn.py", "ok.py"])
                assert pump_until(lambda: False, timeout=0.6) is False
                assert calls == ["warn.py"], calls  # 増えていない
                warn_b = item_of(window_b, "warn.py")
                assert warn_b is not None and warn_b.ran
                assert warn_b.last_run is None
                assert row_cell_texts(window_b, "warn.py")[2] \
                    == "警告・済"
                # 前回の結果の控えがあるので「再実行」(1.56)。
                # 「AIへ回答」にはならない (last_run は無い)。
                assert row_button_texts(window_b, "warn.py") \
                    == ["再実行"]
                # (c) 実行していない分は「OK」のまま。
                ok_b = item_of(window_b, "ok.py")
                assert ok_b is not None and not ok_b.ran
                assert row_cell_texts(window_b, "ok.py")[2] == "OK"
                # (d) 中身を書き換えると「・済」が取れる。自動実行
                # の切替は切に戻してから書き換える (書き換え後の
                # 中身が自動で走って印が付き直すのを避ける)。
                _make_auto_window(tmp, False)
                old_sha = warn_b.result.sha256
                (watch / "warn.py").write_text(
                    "print('書き換え後')\n", encoding="utf-8",
                    newline="")
                window_b._poll()
                window_b._poll()
                assert pump_until(
                    lambda: (item := item_of(window_b, "warn.py"))
                    is not None and item.result is not None
                    and item.result.sha256 != old_sha)
                warn_b2 = item_of(window_b, "warn.py")
                assert warn_b2 is not None and not warn_b2.ran
                assert row_cell_texts(window_b, "warn.py")[2] == "OK"
                assert calls == ["warn.py"], calls
            finally:
                window_b.close()
    finally:
        core.execute_script = original_execute
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_diag_events_recorded_and_bundle_button():
    # 診断 (1-41): DL の一連 (表示・押下・到着) とチェック・実行が
    # 控えに残り、開き直しても読み直せること。束には版・状態・
    # 記録が入ること。「AIの窓」の「診断をAIへ貼る」が、束を
    # 表示中プロファイルの入力欄へ入れる (だけ) こと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))
            window.ai_dl_presser = (  # type: ignore[method-assign]
                lambda profile, key=None: sr._AiAnswerResult(True, "ok"))

            def texts():
                return [e["text"] for e in window._diag_events]

            try:
                candidate = {
                    "name": "#!phal python\n"
                            "# 9-5: 診断のテスト用スクリプトです\n"
                            "print(1)\n",
                    "class_name": "cls group/card",
                    "label": "9-5: 診断のテスト用スクリプトです",
                    "summary": "9-5: 診断のテスト用スクリプトです",
                }
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate))
                assert any("DL候補を表示" in t for t in texts())
                window._press_download(auto=False)
                assert pump_until(
                    lambda: not window._dl_pressing
                    and window._watch_list[0].holder is None)
                assert any("DL押下を開始（手動）" in t for t in texts())
                assert any("DL押下に成功" in t for t in texts())
                # 落としたファイルが届く → 到着の確認とチェック。
                write_script(
                    watch, "9-5_install_phal.phal",
                    "#!phal python\n"
                    "# 9-5: 診断のテスト用スクリプトです\n"
                    "print('中身')\n")
                window._poll()
                settle_checks(window, ["9-5_install_phal.phal"])
                assert any("DLの到着を確認" in t for t in texts())
                assert any(t.startswith("チェック: 9-5")
                           for t in texts())
                # 実行も残る。
                item = item_of(window, "9-5_install_phal.phal")
                window._start_run(item)
                assert pump_until(
                    lambda: item.last_run is not None)
                assert any("実行開始: 9-5" in t for t in texts())
                assert any("実行終了: 9-5" in t
                           and "終了コード 0" in t for t in texts())
                # 束: 見出し・プロファイル名・状態・記録が入る。
                bundle = window._diagnostics_text()
                assert bundle.startswith(
                    f"PHAL診断 Ver{sr.PHAL_VERSION} ")
                assert "AIプロファイル: 既定" in bundle
                assert "- DL行: なし" in bundle
                assert "DL押下に成功" in bundle
                # 控えはファイルに残っている (開き直しで読み直せる)。
                saved = core.load_diag_events()
                assert any("DL押下に成功" in e["text"] for e in saved)
            finally:
                window.close()
            # 開き直した窓は、前の記録を引き継いでいる。
            window2 = sr.ScriptRunnerWindow(str(settings_path))
            try:
                assert any("DL押下に成功" in e["text"]
                           for e in window2._diag_events)
            finally:
                window2.close()
        # --- 「診断をAIへ貼る」ボタン ---
        filled: list = []

        def fake_filler(text, profile):
            filled.append((text, profile))
            return sr._AiAnswerResult(True, "入力欄に入れました")

        dialog = sr_ai.SettingsDialog(
            {"process_name": "dummy.exe"}, None,
            filler=fake_filler,
            diag_text_provider=lambda: "束のテキスト")
        try:
            assert dialog.ai_page._diag_button is not None
            dialog.ai_page._diag_button.click()
            assert pump_until(lambda: len(filled) == 1)
            assert filled[0][0] == "束のテキスト"
            assert isinstance(filled[0][1], dict)
        finally:
            dialog.close()
        # 作る役が無ければボタンは出ない。
        plain = sr_ai.SettingsDialog({"process_name": "dummy.exe"}, None)
        try:
            assert plain.ai_page._diag_button is None
        finally:
            plain.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_escape_sent_after_dl_arrival():
    # ESC 送信 (1-67): DL の到着が確かめられた直後に、AI の窓へ
    # ESC を送る役が呼ばれ、結果が診断に残ること。押下だけでは
    # (到着前は) 呼ばれないこと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            esc_calls: list = []

            def fake_esc_sender(profile):
                esc_calls.append(profile)
                return sr._AiAnswerResult(True, "ESC送信: sent")

            window.ai_esc_sender = (  # type: ignore[method-assign]
                fake_esc_sender)
            window.ai_dl_presser = (  # type: ignore[method-assign]
                lambda profile, key=None: sr._AiAnswerResult(True, "ok"))
            try:
                candidate = {
                    "name": "#!phal python\n"
                            "# 9-6: ESC送信のテスト用スクリプトです\n"
                            "print(1)\n",
                    "class_name": "cls group/card",
                    "label": "9-6: ESC送信のテスト用スクリプトです",
                    "summary": "9-6: ESC送信のテスト用スクリプトです",
                }
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate))
                window._press_download(auto=False)
                assert pump_until(
                    lambda: not window._dl_pressing
                    and window._watch_list[0].holder is None)
                # まだ届いていないので、ESC は送られていない。
                assert esc_calls == []
                write_script(
                    watch, "9-6_install_phal.phal",
                    "#!phal python\n"
                    "# 9-6: ESC送信のテスト用スクリプトです\n"
                    "print('中身')\n")
                window._poll()
                settle_checks(window, ["9-6_install_phal.phal"])
                assert pump_until(
                    lambda: any(
                        "ESC送信: sent" in e["text"]
                        for e in window._diag_events))
                assert len(esc_calls) == 1
                assert any("DLの到着を確認" in e["text"]
                           for e in window._diag_events)
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_diag_auto_response():
    # 診断の自動応答 (1-43):
    # (a) 切替が入っていて目印が来たら、束を入れて送信まで自動で
    #     やり、消費が控え (ファイル) に残ること。同じ目印では
    #     二度送らず、開き直しても消費が効くこと。
    # (b) 間隔 (5分) が空いていなければ見送り、空いたら送ること。
    # (c) 連鎖の途中では見送り、空いてから送ること。
    # (d) 切替が切なら何もしないこと (理由は診断に残る)。
    # (e) 入力欄に書きかけがあれば、上書きせず見送って記録する
    #     こと (送信は押されない)。
    # (f) 同じ目印が会話の木のずれで id だけ変わって再来しても、
    #     key が一致すれば二度送らない (1-49)。
    # (g) 文言が違う別目印は従来どおり送ること。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    try:
        # --- (a)(b)(c) 切替あり ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_diag=True,
                ai_target={"process_name": "dummy.exe"})
            fills: list = []
            presses: list = []

            def attach(window):
                window._set_clipboard = (  # type: ignore[method-assign]
                    lambda text: setattr(
                        window, "_last_clipboard_text", text))
                window.ai_downloader = (  # type: ignore[method-assign]
                    lambda profile: sr._DlCheckResult(
                        False, "なし", None))

                def fake_sender(text, profile):
                    fills.append(text)
                    return sr._AiAnswerResult(True, "入れました")

                def fake_presser(profile):
                    presses.append(profile)
                    return sr._AiAnswerResult(True, "押しました")

                window.ai_sender = fake_sender  # type: ignore
                window.ai_presser = fake_presser  # type: ignore

            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window)
            try:
                assert window._automation_flags()["auto_diag"] is True
                marker1 = {"id": "marker-1"}
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=marker1))
                assert pump_until(lambda: len(presses) == 1)
                assert len(fills) == 1
                assert fills[0].startswith(
                    f"PHAL診断 Ver{sr.PHAL_VERSION} ")
                # 完了の知らせは、裏方の完了シグナルが届いてから
                # 出る (押下の記録だけ先に立つことがある)。
                assert pump_until(
                    lambda: window.note.text()
                    == "診断を自動で送りました")
                state = core.load_diag_auto()
                assert state["consumed_id"] == "marker-1"
                # key の無い従来の目印では、key の控えは空のまま。
                assert state["consumed_key"] == ""
                assert state["last_sent_at"] > 0
                # 同じ目印では二度送らない。
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=marker1))
                settle()
                assert len(fills) == 1 and len(presses) == 1
            finally:
                window.close()
            # 開き直しても消費が効く (控えはファイルに残っている)。
            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window)
            try:
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=marker1))
                settle()
                assert len(fills) == 1 and len(presses) == 1
                # (b) 別の目印でも、間隔が空くまでは見送る。
                marker2 = {"id": "marker-2"}
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=marker2))
                settle()
                assert len(fills) == 1
                assert window._diag_marker_pending == "marker-2"
                assert any("直近に送ったばかり" in e["text"]
                           for e in window._diag_events)
                # 間隔が空いたら、見張りの次の回で送る。
                window._diag_auto["last_sent_at"] = (
                    time.time() - core.DIAG_AUTO_COOLDOWN_SEC - 10)
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=marker2))
                assert pump_until(lambda: len(presses) == 2)
                # 消費の書き出しは完了シグナル側 (GUI スレッド) の
                # 仕事なので、ファイルに残るまで待つ。
                assert pump_until(
                    lambda: core.load_diag_auto()["consumed_id"]
                    == "marker-2")
                # (c) 連鎖の途中では見送り、空いてから送る。
                window._diag_auto["last_sent_at"] = (
                    time.time() - core.DIAG_AUTO_COOLDOWN_SEC - 10)
                marker3 = {"id": "marker-3", "key": "key-3"}
                window._ai_filling = True
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=marker3))
                settle()
                assert len(fills) == 2
                assert window._diag_marker_pending == "marker-3"
                assert any("連鎖の途中" in e["text"]
                           for e in window._diag_events)
                window._ai_filling = False
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=marker3))
                assert pump_until(lambda: len(presses) == 3)
                # 消費の書き出しは完了シグナル側 (GUI スレッド) の
                # 仕事なので、ファイルに残るまで待つ。
                assert pump_until(
                    lambda: core.load_diag_auto()["consumed_key"]
                    == "key-3")
                # (f) 同じ目印が会話の木のずれで id だけ変わって
                #     再来しても、key が一致すれば二度送らない。
                #     間隔は空けておく (空いていなければ間隔の見送
                #     りでも止まってしまい、key の判定を見られない)。
                window._diag_auto["last_sent_at"] = (
                    time.time() - core.DIAG_AUTO_COOLDOWN_SEC - 10)
                drifted = {"id": "marker-3-drifted", "key": "key-3"}
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=drifted))
                settle()
                assert len(fills) == 3 and len(presses) == 3
                # (g) 文言が違う別目印は従来どおり送る。
                marker4 = {"id": "marker-4", "key": "key-4"}
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", None, marker=marker4))
                assert pump_until(lambda: len(presses) == 4)
                assert pump_until(
                    lambda: core.load_diag_auto()["consumed_key"]
                    == "key-4")
            finally:
                window.close()
        # --- (d) 切替なし ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            fills2: list = []
            window.ai_sender = (  # type: ignore[method-assign]
                lambda text, profile: fills2.append(text)
                or sr._AiAnswerResult(True, "入れました"))
            try:
                assert window._automation_flags()["auto_diag"] is False
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, marker={"id": "marker-off"}))
                settle()
                assert fills2 == []
                assert any("自動応答は切のため見送り" in e["text"]
                           for e in window._diag_events)
            finally:
                window.close()
        # --- (e) 書きかけがあれば見送る ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_diag=True,
                ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            presses3: list = []
            window.ai_sender = (  # type: ignore[method-assign]
                lambda text, profile: sr._AiAnswerResult(
                    False, "入力欄に書きかけの文があります",
                    needs_confirm=True))
            window.ai_presser = (  # type: ignore[method-assign]
                lambda profile: presses3.append(profile)
                or sr._AiAnswerResult(True, "押しました"))
            try:
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, marker={"id": "marker-draft"}))
                assert pump_until(
                    lambda: core.load_diag_auto()["consumed_id"]
                    == "marker-draft")
                assert presses3 == []  # 送信は押されていない
                assert any("書きかけ" in e["text"]
                           for e in window._diag_events)
                assert "書きかけがあるため" in window.note.text()
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_reset_auto_response():
    # リセットの合図 (1-52):
    # (a) 切替が入っていて目印が来たら、開き直しの頼みが1回だけ
    #     届き、消費と時刻が控え (ファイル) に残ること。同じ目印
    #     では二度反応せず (id が漂っても key が一致すれば同じ)、
    #     開き直しても消費が効くこと。診断の控えには触らないこと。
    # (b) 別の目印でも、間隔 (10分) が空くまでは反応せず、空い
    #     たら反応すること。
    # (c) 切替が切なら何もしないこと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_reset=True,
                ai_target={"process_name": "dummy.exe"})
            restarts: list = []

            def attach(window):
                window._set_clipboard = (  # type: ignore[method-assign]
                    lambda text: setattr(
                        window, "_last_clipboard_text", text))
                window.ai_downloader = (  # type: ignore[method-assign]
                    lambda profile: sr._DlCheckResult(
                        False, "なし", None))
                window.request_restart = (  # type: ignore[method-assign]
                    lambda: restarts.append(1))

            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window)
            try:
                assert window._automation_flags()["auto_reset"] is True
                marker1 = {"id": "reset-1", "key": "rkey-1"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, reset_marker=marker1))
                assert len(restarts) == 1
                state = core.load_reset_auto()
                assert state["consumed_id"] == "reset-1"
                assert state["consumed_key"] == "rkey-1"
                assert state["last_reset_at"] > 0
                # 診断の控えには触っていない。
                assert core.load_diag_auto()["consumed_id"] == ""
                # 同じ目印では二度反応しない。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, reset_marker=marker1))
                assert len(restarts) == 1
                # id が漂っても key が一致すれば同じ発言。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    reset_marker={"id": "reset-1b", "key": "rkey-1"}))
                assert len(restarts) == 1
                # (b) 別の目印でも、間隔が空くまでは反応しない。
                marker2 = {"id": "reset-2", "key": "rkey-2"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, reset_marker=marker2))
                assert len(restarts) == 1
                # 間隔が空いたら反応する (控えの時刻を過去へずらす)。
                window._reset_auto["last_reset_at"] = (
                    time.time() - core.RESET_AUTO_COOLDOWN_SEC - 1.0)
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, reset_marker=marker2))
                assert len(restarts) == 2
                assert core.load_reset_auto()["consumed_id"] \
                    == "reset-2"
            finally:
                window.close()
            # 開き直しても消費が効く (控えはファイルに残っている)。
            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window)
            try:
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    reset_marker={"id": "reset-2", "key": "rkey-2"}))
                assert len(restarts) == 2
            finally:
                window.close()
        # (c) 切替が切なら何もしない。
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            restarts_off: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window.request_restart = (  # type: ignore[method-assign]
                lambda: restarts_off.append(1))
            try:
                assert window._automation_flags()["auto_reset"] is False
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    reset_marker={"id": "reset-9", "key": "rkey-9"}))
                assert restarts_off == []
                assert core.load_reset_auto()["consumed_id"] == ""
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_blocked_notice():
    # チェックで止まった知らせ (1-53):
    # (a) auto_run 入り・auto_ack 既定 (入) で、起動後に届いた
    #     ファイルがチェックで実行不可になったら、理由つきの
    #     1行が1度だけ出ること。開き直しても、同じ中身では
    #     控えが効いて出ないこと。
    # (b) auto_ack が切なら出ないこと (控えにも残らない)。
    # (c) auto_run が切なら出ないこと (控えにも残らない)。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    def attach(window, fills, presses):
        window._set_clipboard = (  # type: ignore[method-assign]
            lambda text: setattr(
                window, "_last_clipboard_text", text))
        window.ai_downloader = (  # type: ignore[method-assign]
            lambda profile: sr._DlCheckResult(False, "なし", None))

        def fake_sender(text, profile):
            fills.append(text)
            return sr._AiAnswerResult(True, "入れました")

        def fake_presser(profile):
            presses.append(profile)
            return sr._AiAnswerResult(True, "押しました")

        window.ai_sender = fake_sender  # type: ignore
        window.ai_presser = fake_presser  # type: ignore

    def drop_blocked(window, watch):
        write_script(
            watch, "9-9_blocked.py",
            "import traytools_no_such_module_zzz\nprint(1)\n")
        window._poll()
        window._poll()
        settle_checks(window, ["9-9_blocked.py"])
        item = item_of(window, "9-9_blocked.py")
        assert item is not None
        assert item.status == core.STATUS_ERROR
        return item

    try:
        # --- (a) 知らせが出て、1度だけ ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, auto_run=True,
                ai_target={"process_name": "dummy.exe"})
            fills: list = []
            presses: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window, fills, presses)
            try:
                flags = window._automation_flags()
                assert flags["auto_run"] is True
                assert flags["auto_ack"] is True  # 既定で入
                item = drop_blocked(window, watch)
                assert pump_until(lambda: len(fills) == 1)
                assert fills[0].startswith(
                    "止まりました：9-9 9-9_blocked.py "
                    "はチェックで実行不可（"), fills
                assert "traytools_no_such_module_zzz" in fills[0]
                assert pump_until(lambda: len(presses) == 1)
                # 控え (ファイル) に中身のハッシュが残る。
                records = core.load_blocked_notified()
                assert core.blocked_notified_contains(
                    records, item.result.sha256)
                # チェック完了の入口を通し直しても二度出ない。
                window._admit_auto_run(item)
                settle()
                assert len(fills) == 1 and len(presses) == 1
            finally:
                window.close()
            # 開き直しても、控えが効いて出ない (対象の控えで
            # auto_eligible は引き継がれるが、知らせ済みの控えが
            # 先に止める)。
            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window, fills, presses)
            try:
                window._poll()
                settle_checks(window, ["9-9_blocked.py"])
                settle()
                assert len(fills) == 1 and len(presses) == 1
            finally:
                window.close()
        # --- (b) auto_ack 切なら出ない ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, auto_run=True, auto_ack=False,
                ai_target={"process_name": "dummy.exe"})
            fills_off: list = []
            presses_off: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window, fills_off, presses_off)
            try:
                assert window._automation_flags()["auto_run"] is True
                assert window._automation_flags()["auto_ack"] is False
                drop_blocked(window, watch)
                settle()
                assert fills_off == [] and presses_off == []
                assert core.load_blocked_notified() == {}
            finally:
                window.close()
        # --- (c) auto_run 切なら出ない ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            fills_run_off: list = []
            presses_run_off: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window, fills_run_off, presses_run_off)
            try:
                assert window._automation_flags()["auto_run"] is False
                assert window._automation_flags()["auto_ack"] is True
                drop_blocked(window, watch)
                settle()
                assert fills_run_off == [] and presses_run_off == []
                assert core.load_blocked_notified() == {}
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dark_mode_toggle_and_startup():
    # ダークモード (1-44): テーマ表のライトの値が従来定数と
    # 同じこと、「ダーク」の切替でその場でパレットが替わって
    # 設定に残ること、dark_mode 入りで起動すると最初から
    # ダークで開いて切替も入っていること、親がダークだと
    # 「AIの窓」もダークのパレットを受け継ぐことを見る。
    light = sr.THEMES["light"]
    assert light["status"] == sr.STATUS_COLORS
    assert light["status"][core.STATUS_OK] == "#1f7a45"
    assert light["status"][core.STATUS_ERROR] == "#b3342d"
    assert light["status"][core.STATUS_WARN] == "#8a5a00"
    assert light["duplicate"] == sr.DUPLICATE_COLOR == "#c2512f"
    assert light["note_styles"] == sr.NOTE_STYLES
    assert light["note_styles"]["error"] == \
        "color: #b3342d; background: #fbeceb;"
    assert light["accent_style"] == \
        "background: #1f6f5c; color: #ffffff;"
    assert light["selection_tint"] == (0, 120, 215, 70)
    assert sr._SELECTION_TINT == QColor(0, 120, 215, 70)
    assert light["muted"] == "#5b665f" and light["link"] == "#1a5fb4"
    assert light["dialog_error"] == "#c00000"
    assert light["chrome"]["window"] == "#f2f3f5"
    assert light["chrome"]["base"] == "#ffffff"
    dark = sr.THEMES["dark"]
    # 意味のある色は、ダークでも互いに (とライトと) 区別できる。
    dark_semantics = {
        dark["status"][core.STATUS_OK], dark["status"][core.STATUS_ERROR],
        dark["status"][core.STATUS_WARN], dark["duplicate"]}
    assert len(dark_semantics) == 4

    window_role = QPalette.ColorRole.Window
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, _watch, settings_path, _copied = make_window(tmp)
        try:
            # 既定はライトで、専用の明るい配色が当たる (1.78)。
            assert not window.dark_box.isChecked()
            assert window.styleSheet() != ""
            assert window.palette().color(window_role).name() == \
                light["chrome"]["window"]
            assert not window.table.alternatingRowColors()
            # 切替を入れると、その場でダークになり設定にも残る。
            window.dark_box.setChecked(True)
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            assert saved["script_runner"]["dark_mode"] is True
            assert window.styleSheet() != ""
            assert window.palette().color(window_role).name() == \
                dark["chrome"]["window"]
            assert window.table.alternatingRowColors()
            assert window._delegate.tint == QColor(
                *dark["selection_tint"])
            window._set_note("試し", "error")
            assert window.note.styleSheet() == \
                dark["note_styles"]["error"]
            # 戻すとライトに復帰し、設定も戻る。
            window.dark_box.setChecked(False)
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            assert saved["script_runner"]["dark_mode"] is False
            assert window.styleSheet() != ""
            assert window.palette().color(window_role).name() == \
                light["chrome"]["window"]
            assert not window.table.alternatingRowColors()
            window._set_note("試し", "error")
            assert window.note.styleSheet() == \
                sr.NOTE_STYLES["error"]
        finally:
            window.close()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        watch = tmp / "watch"
        watch.mkdir()
        settings_path = tmp / "settings.json"
        data = json.loads(json.dumps(settings_mod.DEFAULT_SETTINGS))
        data["script_runner"] = {
            "watch_folder": str(watch),
            "python": sys.executable,
            "timeout_seconds": 10,
            "hide_no_id": False,
            "dark_mode": True,
        }
        settings_path.write_text(json.dumps(data, ensure_ascii=False),
                                 encoding="utf-8")
        window = sr.ScriptRunnerWindow(str(settings_path))
        try:
            # 起動時からダークで、切替も入った状態で開く。
            assert window.config["dark_mode"] is True
            assert window.dark_box.isChecked()
            assert window.styleSheet() != ""
            assert window.palette().color(window_role).name() == \
                dark["chrome"]["window"]
            # 親 (PHAL の窓) を継いで開くダイアログもダークになる。
            # パレットは子ダイアログへ受け継がれないので、窓の
            # スタイルシートが効いて地が暗くなっていることを、
            # 描画した画素で確かめる。
            dialog = sr_ai.SettingsDialog(
                {"process_name": "dummy.exe"}, window,
                error_color=window._theme()["dialog_error"])
            try:
                assert dialog.ai_page._error_color == "#ff7b72"
                dialog.show()
                QCoreApplication.processEvents()
                image = dialog.grab().toImage()
                pixel = image.pixelColor(4, image.height() - 4)
                assert pixel.name() == dark["chrome"]["window"], pixel
            finally:
                dialog.close()
        finally:
            window.close()


def test_intro_button_falls_back_when_unconfigured():
    # 「PHALループ開始プロンプトを入力」(1.82、旧「…を送信」1.77):
    # 投げ先が未設定なら送らず、開始プロンプトをクリップボードへ
    # 入れてダイアログで知らせる。VM では PowerShell が無いので、
    # 環境のその項目は「不明」になる。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _settings_path, copied = make_window(tmp)
        dialogs = []

        class _FakeBox:
            @staticmethod
            def information(parent, title, text):
                dialogs.append(text)

            @staticmethod
            def warning(parent, title, text):
                dialogs.append(text)

        original_box = sr.QMessageBox
        sr.QMessageBox = _FakeBox

        class _FakeFileDialog:
            @staticmethod
            def getExistingDirectory(parent, title, start):
                return str(watch)

        original_fd = sr.QFileDialog
        sr.QFileDialog = _FakeFileDialog
        try:
            assert window.intro_button.text() == \
                "PHALループ開始プロンプトを入力"
            assert window.ai_window_button.text() == "設定"
            window.intro_button.click()
            QCoreApplication.processEvents()
            assert len(copied) == 1, copied
            text = copied[0]
            assert text.startswith("PHAL のループを始めます。"), text
            # 作業フォルダの行に、ダイアログで選んだものが入る (1.83)
            assert f"- 作業フォルダ: {watch}" in text, text
            assert "直接出られません" in text, text
            assert "短いスクリプト" in text, text
            assert str(watch) in text, text
            assert "- PowerShell: 不明" in text, text
            assert dialogs and "クリップボード" in dialogs[0], dialogs
            assert "開始プロンプトをクリップボードに入れました" \
                in window.note.text(), window.note.text()
        finally:
            sr.QMessageBox = original_box
            sr.QFileDialog = original_fd
            window.close()


def test_intro_button_cancelled_folder_dialog():
    # 作業フォルダのダイアログをキャンセルしたら、開始プロンプト
    # は入れない (1.83)。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _settings_path, copied = make_window(tmp)

        class _FakeFileDialog:
            @staticmethod
            def getExistingDirectory(parent, title, start):
                return ""

        original_fd = sr.QFileDialog
        sr.QFileDialog = _FakeFileDialog
        try:
            window.intro_button.click()
            QCoreApplication.processEvents()
            assert copied == [], copied
            assert "選ばれなかった" in window.note.text(), \
                window.note.text()
        finally:
            sr.QFileDialog = original_fd
            window.close()


def test_intro_auto_text_zip_line_follows_profile():
    # 開始プロンプトの ZIP の行は、有効なプロファイルで
    # zip_as_txt が入っているときだけ出る (1.81 の配線を
    # 実経路で確かめる。1.83 で発見した未配線の直し)。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, settings_path, copied = make_window(tmp)
        assert ".zip.txt" not in window._intro_auto_text()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        data["script_runner"]["ai_profiles"] = [{
            "name": "copilot",
            "target": {"label": "Copilot", "zip_as_txt": True}}]
        settings_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        text = window._intro_auto_text()
        assert ".zip.txt に変えて渡します" in text, text
        window.close()


def test_ai_dialog_intro_prompt_editor():
    # 「設定」画面の開始プロンプト欄 (1.77、2.01 で共通設定
    # タブへ移動): 保存済みの文が入り、「自動生成の文を入れる」
    # で provider の文に替わり、そのまま intro_prompt() で
    # 取り出せる。題は「設定 - PHAL」。
    dialog = sr_ai.SettingsDialog(
        {}, None, common={"intro_prompt": "保存した文"},
        intro_text_provider=lambda: "自動生成の文")
    try:
        assert dialog.windowTitle() == "設定 - PHAL"
        assert dialog.intro_prompt() == "保存した文"
        dialog.common_page._intro_load_button.click()
        assert dialog.intro_prompt() == "自動生成の文"
        # 「保存した文を使う」を外すと、保存内容は空 (自動生成)
        # になる (1.80)。
        dialog.common_page._intro_use_box.setChecked(False)
        assert dialog.intro_prompt() == ""
        # ZIP の名前変え (1.81) はプロファイルの欄として往復する
        assert dialog.ai_page.fields()["zip_as_txt"] is False
        dialog.ai_page._zip_as_txt_box.setChecked(True)
        assert dialog.ai_page.fields()["zip_as_txt"] is True
    finally:
        dialog.close()


def test_record_button_copies_loop_record():
    # 「記録をコピー」(1.51): 表の直下、開始プロンプトの隣。
    # まだ記録が無ければ何も入れず「まだ記録がありません」だけ。
    # 実行が終わった後なら、直近の手を Markdown にして入れる。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        window, watch, _sp, copied = make_window(tmp)
        try:
            assert window.record_button.text() == "記録をコピー"
            window.record_button.click()
            QCoreApplication.processEvents()
            assert copied == []
            assert "まだ記録がありません" in window.note.text()
            write_script(watch, "1-81_check_db.py",
                         "# DB の件数を数える\nprint('count: 42')\n")
            window.show()
            settle_checks(window, ["1-81_check_db.py"])
            select(window, "1-81_check_db.py")
            window._f5_pressed()
            assert pump_until(lambda: copied != []
                              and window._running_item is None)
            copied.clear()
            window.record_button.click()
            QCoreApplication.processEvents()
            assert len(copied) == 1, copied
            text = copied[0]
            assert text.startswith("# PHAL ループ記録（直近1手）"), text
            assert "## 1-81 " in text, text
            assert "- ねらい：DB の件数を数える" in text, text
            assert "- スクリプト：1-81_check_db.py" in text, text
            assert "終了コード 0" in text, text
            assert "count: 42" in text, text
            assert "クリップボードに入れました" in window.note.text()
        finally:
            window.close()


def test_rerun_menu_and_diff():
    # 「再実行」(1.56): 前回の実行結果が控えにある行だけ、右クリック
    # のメニューと行のボタンに「再実行」が出る。選ぶと通常の経路で
    # もう一度実行され、結果文は差分だけになる (全文の節は出ない。
    # 末尾は AI への指示文のまま)。前回の結果は JSON の控えに残り、
    # 開き直しをまたぐ。同じ中身の再実行は、手動なので実行済み
    # ハッシュのガードに掛からず走り、「差分なし」の1行だけになる。
    # ファイルが無くなった行は、その旨を出して止まる。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
        os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
        try:
            window, watch, _sp, copied = make_window(tmp)
            try:
                write_script(watch, "1-90_rerun.py",
                             "print('one')\nprint('two')\n")
                window.show()
                settle_checks(window, ["1-90_rerun.py"])
                item = item_of(window, "1-90_rerun.py")
                # まだ実行していないので「再実行」は出ない。
                assert "再実行" not in menu_action_texts(
                    window, "1-90_rerun.py")
                assert row_button_texts(
                    window, "1-90_rerun.py") == ["実行"]
                # 1回目: 通常の実行。全文の結果文。
                select(window, "1-90_rerun.py")
                window._f5_pressed()
                assert pump_until(lambda: copied != []
                                  and window._running_item is None)
                assert "one" in copied[-1] and "two" in copied[-1]
                assert "--- 出力 ---" in copied[-1], copied[-1]
                assert copied[-1].endswith(core.RESULT_AI_PROMPT)
                # 前回の結果が JSON の控えに残っている (鍵は ID)。
                stored = json.loads(
                    (tmp / "data" / "phal_last_results.json").read_text(
                        encoding="utf-8"))
                assert "1-90" in stored, stored
                assert "two" in stored["1-90"]["text"], stored
                assert stored["1-90"]["truncated"] is False
                # 実行後はメニューに「再実行」が出る。サイクルが
                # 回ったことにすると、行のボタンも「再実行」になる。
                assert "再実行" in menu_action_texts(
                    window, "1-90_rerun.py")
                item.answered = True
                item.sent = True
                window._refresh_list()
                assert row_button_texts(
                    window, "1-90_rerun.py") == ["再実行"]
                # 出力が変わるように中身を差し替える。上書きは
                # 再チェックになるので、新しい中身のチェック結果
                # が付くまで待つ (古い結果のままだと、実行の直前
                # の sha 照合で再チェックへ戻される)。ボタンは
                # 控えがあるので「再実行」のまま。
                copied.clear()
                old_sha = item.result.sha256
                write_script(watch, "1-90_rerun.py",
                             "print('one')\nprint('three')\n")
                assert pump_until(
                    lambda: item_of(window, "1-90_rerun.py").result
                    is not None
                    and item_of(window, "1-90_rerun.py").result.sha256
                    != old_sha)
                assert row_button_texts(
                    window, "1-90_rerun.py") == ["再実行"]
                for index, it in enumerate(window._items):
                    if it.name == "1-90_rerun.py":
                        button = window.table.cellWidget(index, sr.COL_RUN)
                assert isinstance(button, QPushButton)
                assert button.text() == "再実行"
                QTest.mouseClick(button, Qt.LeftButton)
                assert pump_until(lambda: copied != []
                                  and window._running_item is None)
                text = copied[-1]
                assert text.startswith(
                    "[実行結果] 1-90 1-90_rerun.py"), text
                assert "差分：追加 1行・削除 1行（前回比）" in text, text
                assert "+ three" in text, text
                assert "- two" in text, text
                assert "one" not in text, text
                assert "--- 出力 ---" not in text, text
                assert text.endswith(core.RESULT_AI_PROMPT), text
                # 同じ中身で再実行すると「差分なし」の1行だけに
                # なる (実行済みハッシュのガードに掛からず走る)。
                item = item_of(window, "1-90_rerun.py")
                item.answered = True
                item.sent = True
                window._refresh_list()
                assert row_button_texts(
                    window, "1-90_rerun.py") == ["再実行"]
                copied.clear()
                for index, it in enumerate(window._items):
                    if it.name == "1-90_rerun.py":
                        button = window.table.cellWidget(index, sr.COL_RUN)
                QTest.mouseClick(button, Qt.LeftButton)
                assert pump_until(lambda: copied != []
                                  and window._running_item is None)
                lines = copied[-1].split("\n")
                assert len(lines) == 3, copied[-1]
                assert lines[0].startswith(
                    "[実行結果] 1-90 1-90_rerun.py"), copied[-1]
                assert lines[1] == "差分なし（前回と同じ結果）", copied[-1]
                assert lines[2] == core.RESULT_AI_PROMPT, copied[-1]
                # ファイルが無くなった行: その旨を出して止まる。
                copied.clear()
                (watch / "1-90_rerun.py").unlink()
                window._rerun_item(item)
                assert window._running_item is None
                assert copied == []
                assert "ファイルが無くなっているため、再実行できません" \
                    in window.note.text(), window.note.text()
            finally:
                window.close()
        finally:
            if prev_data_dir is None:
                os.environ.pop("TRAYTOOLS_DATA_DIR", None)
            else:
                os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_answer_with_attachments():
    # 添付 (1.58):
    # - スクリプトが標準出力に「添付：ファイル名」を出すと、
    #   結果文 (クリップボード文) にその行は残らず、項目に
    #   添付名が残る。
    # - 「AIへ回答」では、outbox に実在するファイルだけが
    #   送信役へ attachments として渡り、無いファイルは
    #   添付されず、文章の末尾に「添付できませんでした：名前」
    #   が足される。
    # - 送信役の結果に付いた添付の結果は、診断に残る。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
        try:
            settings_path, watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            copied: list = []
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: copied.append(text)
                or setattr(window, "_last_clipboard_text", text))
            sent: list = []

            def fake_sender(text, profile, attachments=None):
                sent.append((text, attachments))
                return sr._AiAnswerResult(
                    True, "入れました", attach_done=["result.csv"])

            window.ai_sender = fake_sender  # type: ignore
            try:
                # 置き場に実在するファイル (スクリプトが書いた
                # 成果ファイルのつもり) を用意しておく。
                outbox = core.outbox_dir()
                outbox.mkdir(parents=True, exist_ok=True)
                (outbox / "result.csv").write_text(
                    "a,b\n1,2\n", encoding="utf-8")
                write_script(
                    watch, "9-7_attach.py",
                    "print('集計しました')\n"
                    "print('添付：result.csv')\n"
                    "print('添付：missing.txt')\n"
                    "print('以上です')\n")
                window._poll()
                settle_checks(window, ["9-7_attach.py"])
                select(window, "9-7_attach.py")
                window._f5_pressed()
                assert pump_until(lambda: copied != []
                                  and window._running_item is None)
                text = copied[-1]
                assert "集計しました" in text and "以上です" in text
                assert "添付：" not in text, text  # 指定の行は残らない
                item = item_of(window, "9-7_attach.py")
                assert item.attach_names == ["result.csv", "missing.txt"]
                # 回答: 実在するものだけが attachments で渡る。
                window._answer_to_ai(item)
                assert pump_until(lambda: item.answered)
                assert len(sent) == 1
                sent_text, sent_attachments = sent[0]
                assert sent_attachments == [str(outbox / "result.csv")]
                assert "添付：result.csv" not in sent_text
                assert sent_text.endswith(
                    "\n添付できませんでした：missing.txt"), sent_text
                # 診断に、添付の結果と、無かったファイルの件が残る。
                diag_texts = [e["text"] for e in window._diag_events]
                assert any("添付した: result.csv" in t
                           for t in diag_texts), diag_texts
                assert any("添付の指定があったがファイルが無い: "
                           "missing.txt" in t
                           for t in diag_texts), diag_texts
            finally:
                window.close()
        finally:
            if prev_data_dir is None:
                os.environ.pop("TRAYTOOLS_DATA_DIR", None)
            else:
                os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_settings_manual_auto_response():
    # 設定確認・マニュアル再送の自動応答 (1.85):
    # - 切替が入っていて目印が来たら、設定の報告／マニュアルの
    #   添付を入れて送信まで自動でやること。消費が控えに残り、
    #   同じ目印では二度送らないこと。
    # - 切替が切なら何も送らないこと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_settings=True, auto_manual=True,
                ai_target={"process_name": "dummy.exe"})
            fills: list = []
            presses: list = []

            def attach(window):
                window._set_clipboard = (  # type: ignore[method-assign]
                    lambda text: setattr(
                        window, "_last_clipboard_text", text))
                window.ai_downloader = (  # type: ignore[method-assign]
                    lambda profile: sr._DlCheckResult(
                        False, "なし", None))

                def fake_sender(text, profile, attachments=None):
                    fills.append((text, list(attachments or [])))
                    return sr._AiAnswerResult(True, "入れました")

                def fake_presser(profile):
                    presses.append(profile)
                    return sr._AiAnswerResult(True, "押しました")

                window.ai_sender = fake_sender  # type: ignore
                window.ai_presser = fake_presser  # type: ignore

            window = sr.ScriptRunnerWindow(str(settings_path))
            attach(window)
            try:
                flags = window._automation_flags()
                assert flags["auto_settings"] is True
                assert flags["auto_manual"] is True
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    settings_marker={"id": "s-1"}))
                assert pump_until(lambda: len(presses) == 1)
                assert fills[0][0].startswith("PHAL設定 Ver")
                assert "監視フォルダ:" in fills[0][0]
                assert fills[0][1] == []
                assert pump_until(
                    lambda: core.load_settings_auto()[
                        "consumed_id"] == "s-1")
                # 同じ目印では二度送らない。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    settings_marker={"id": "s-1"}))
                settle()
                assert len(fills) == 1
                # マニュアル再送: 添付つきで送る。
                manual = tmp / "phal-ai-manual.md"
                manual.write_text("# manual\n", encoding="utf-8")
                window._intro_manual_path = (  # type: ignore
                    lambda: manual)
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, manual_marker={"id": "m-1"}))
                assert pump_until(lambda: len(presses) == 2)
                assert fills[1][1] == [str(manual)]
                assert "再送します" in fills[1][0]
                assert pump_until(
                    lambda: core.load_manual_auto()[
                        "consumed_id"] == "m-1")
            finally:
                window.close()
        # 切替が切なら送らない。
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            fills2: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender2(text, profile, attachments=None):
                fills2.append(text)
                return sr._AiAnswerResult(True, "入れました")

            window.ai_sender = fake_sender2  # type: ignore
            window.ai_presser = (  # type: ignore[method-assign]
                lambda profile: sr._AiAnswerResult(True, "押しました"))
            try:
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    settings_marker={"id": "s-9"}))
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, manual_marker={"id": "m-9"}))
                settle()
                assert fills2 == []
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_file_auto_response():
    # ファイルリクエストの自動応答 (1.88):
    # - 配下のファイルは添付して送り、消費が控えに残ること。
    # - 範囲外 (フォルダの外) は文だけで知らせること。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, auto_file=True,
                ai_target={"process_name": "dummy.exe"})
            target_file = watch / "data.log"
            target_file.write_text("log body\n", encoding="utf-8")
            fills: list = []
            presses: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender(text, profile, attachments=None):
                fills.append((text, list(attachments or [])))
                return sr._AiAnswerResult(True, "入れました")

            def fake_presser(profile):
                presses.append(profile)
                return sr._AiAnswerResult(True, "押しました")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                assert window._automation_flags()[
                    "auto_file"] is True
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, file_marker={
                        "id": "f-1", "path": "data.log"}))
                assert pump_until(lambda: len(presses) == 1)
                assert fills[0][1] == [str(target_file.resolve())]
                assert "送ります" in fills[0][0]
                assert pump_until(
                    lambda: core.load_file_auto()[
                        "consumed_id"] == "f-1")
                # 範囲外は添付なしの文だけ (間隔の歯止めは
                # テストでは外す)。
                window._file_auto["last_sent_at"] = 0.0
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, file_marker={
                        "id": "f-2",
                        "path": str(tmp / "outside.txt")}))
                assert pump_until(lambda: len(presses) == 2)
                assert fills[1][1] == []
                assert "外なので送れません" in fills[1][0]
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_fulltext_auto_response():
    # 全文リクエストの自動応答 (1.91):
    # - phal-outputs にある全文ファイルは添付して送り、消費が
    #   控えに残ること。
    # - 見つからない ID は文だけで知らせること。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_fulltext=True,
                ai_target={"process_name": "dummy.exe"})
            outputs = tmp / "data" / "phal-outputs"
            outputs.mkdir(parents=True)
            full = outputs / "1-123_20260101-120000_deadbeef.txt"
            full.write_text("全文です\n", encoding="utf-8")
            fills: list = []
            presses: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender(text, profile, attachments=None):
                fills.append((text, list(attachments or [])))
                return sr._AiAnswerResult(True, "入れました")

            def fake_presser(profile):
                presses.append(profile)
                return sr._AiAnswerResult(True, "押しました")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                assert window._automation_flags()[
                    "auto_fulltext"] is True
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, fulltext_marker={
                        "id": "ft-1", "command_id": "1-123"}))
                assert pump_until(lambda: len(presses) == 1)
                assert fills[0][1] == [str(full)]
                assert "全文を送ります" in fills[0][0]
                assert "1-123" in fills[0][0]
                assert pump_until(
                    lambda: core.load_fulltext_auto()[
                        "consumed_id"] == "ft-1")
                # 見つからない ID は添付なしの文だけ (間隔の
                # 歯止めはテストでは外す)。
                window._fulltext_auto["last_sent_at"] = 0.0
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, fulltext_marker={
                        "id": "ft-2", "command_id": "9-999"}))
                assert pump_until(lambda: len(presses) == 2)
                assert fills[1][1] == []
                assert "見つかりませんでした" in fills[1][0]
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_settings_refire_suppression():
    # 合図の再応答の抑止 (1.95、2.00 で作り替え、設定フロー):
    # - 応答すると、控えに last_payload と last_answered_at が
    #   残ること。
    # - 同定子 (consumed_id/key) が外れた状態で同じ合図が再び
    #   見えても、15分以内は二度送らないこと。
    # - 15分を過ぎても繰り返しは送らず、「応答済み」の知らせ
    #   が1回だけ届くこと。以後の再検出は何も送らないこと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_settings=True,
                ai_target={"process_name": "dummy.exe"})
            fills: list = []
            presses: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender(text, profile, attachments=None):
                fills.append((text, list(attachments or [])))
                return sr._AiAnswerResult(True, "入れました")

            def fake_presser(profile):
                presses.append(profile)
                return sr._AiAnswerResult(True, "押しました")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    settings_marker={"id": "s-1"}))
                assert pump_until(lambda: len(presses) == 1)
                assert pump_until(
                    lambda: core.load_settings_auto()[
                        "last_answered_at"] > 0)
                state = core.load_settings_auto()
                assert state["last_payload"] == ""
                assert state["consumed_id"] == "s-1"
                # 同定子が外れた再検出を再現する (消費を消し、
                # 60秒の間隔の歯止めも外す。止めるのは抑止だけ)。
                window._settings_auto["consumed_id"] = ""
                window._settings_auto["consumed_key"] = ""
                window._settings_auto["last_sent_at"] = 0.0
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    settings_marker={"id": "s-2"}))
                settle()
                assert len(fills) == 1
                # 15分を過ぎても、同じ合図は繰り返し送らない
                # (2.00)。代わりに「応答済み」の知らせが1回
                # だけ届く。
                window._settings_auto["last_answered_at"] = (
                    time.time() - 901)
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    settings_marker={"id": "s-3"}))
                assert pump_until(lambda: len(presses) == 2)
                assert "応答済み" in fills[1][0]
                assert "PHAL設定" not in fills[1][0]
                assert pump_until(
                    lambda: core.load_settings_auto().get(
                        "refire_notice_at", 0) > 0)
                # 知らせ済みの再検出は、もう何も送らない。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    settings_marker={"id": "s-4"}))
                settle()
                assert len(fills) == 2
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_fulltext_refire_suppression():
    # 合図の再応答の抑止 (1.95、全文フロー):
    # - 応答すると、控えの last_payload にコマンド ID が残る
    #   こと。
    # - 同定子が外れても、同じ ID は15分以内は二度送らない
    #   こと。違う ID は抑止されないこと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_fulltext=True,
                ai_target={"process_name": "dummy.exe"})
            outputs = tmp / "data" / "phal-outputs"
            outputs.mkdir(parents=True)
            full_a = outputs / "1-123_20260101-120000_deadbeef.txt"
            full_a.write_text("全文A\n", encoding="utf-8")
            full_b = outputs / "1-124_20260101-120000_beefdead.txt"
            full_b.write_text("全文B\n", encoding="utf-8")
            fills: list = []
            presses: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender(text, profile, attachments=None):
                fills.append((text, list(attachments or [])))
                return sr._AiAnswerResult(True, "入れました")

            def fake_presser(profile):
                presses.append(profile)
                return sr._AiAnswerResult(True, "押しました")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, fulltext_marker={
                        "id": "ft-1", "command_id": "1-123"}))
                assert pump_until(lambda: len(presses) == 1)
                assert fills[0][1] == [str(full_a)]
                assert pump_until(
                    lambda: core.load_fulltext_auto()[
                        "last_payload"] == "1-123")
                # 同定子が外れた再検出 (同じ ID) は抑止される。
                window._fulltext_auto["consumed_id"] = ""
                window._fulltext_auto["consumed_key"] = ""
                window._fulltext_auto["last_sent_at"] = 0.0
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, fulltext_marker={
                        "id": "ft-2", "command_id": "1-123"}))
                settle()
                assert len(fills) == 1
                # 違う ID は抑止されない。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, fulltext_marker={
                        "id": "ft-3", "command_id": "1-124"}))
                assert pump_until(lambda: len(presses) == 2)
                assert fills[1][1] == [str(full_b)]
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_history_auto_response():
    # 実行一覧の自動応答 (1.92、素材は実行ログ 1.94):
    # - 切替が入っていて目印が来たら、実行ログから作った
    #   一覧 (新しい順) を送り、消費が控えに残る
    #   こと。同じ目印では二度送らないこと。
    # - 切替が切なら何も送らないこと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_history=True,
                ai_target={"process_name": "dummy.exe"})
            run_log = Path(os.environ["TRAYTOOLS_DATA_DIR"]) / \
                core.LOG_FILENAME
            run_log.parent.mkdir(parents=True, exist_ok=True)
            run_log.write_text(
                json.dumps(
                    {"time": "2026-11-15T16:20:37",
                     "file": "1-201_a.phal", "exit_code": 0,
                     "seconds": 1.3}, ensure_ascii=False) + "\n"
                + json.dumps(
                    {"time": "2026-11-15T16:21:24",
                     "file": "1-202_b.phal", "exit_code": 1,
                     "seconds": 0.1}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            fills: list = []
            presses: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender(text, profile, attachments=None):
                fills.append((text, list(attachments or [])))
                return sr._AiAnswerResult(True, "入れました")

            def fake_presser(profile):
                presses.append(profile)
                return sr._AiAnswerResult(True, "押しました")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                assert window._automation_flags()[
                    "auto_history"] is True
                # 間隔の歯止めはテストでは外す。
                window._history_auto["last_sent_at"] = 0.0
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, history_marker={"id": "h-1"}))
                assert pump_until(lambda: len(presses) == 1)
                sent_lines = fills[0][0].splitlines()
                assert sent_lines[0] == "直近の実行一覧（新しい順）"
                assert "1-202_b.phal" in sent_lines[1]
                assert "終了コード 1" in sent_lines[1]
                assert "1-201_a.phal" in sent_lines[2]
                assert len(sent_lines) == 3
                assert fills[0][1] == []
                assert pump_until(
                    lambda: core.load_history_auto()[
                        "consumed_id"] == "h-1")
                # 同じ目印では二度送らない。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, history_marker={"id": "h-1"}))
                settle()
                assert len(fills) == 1
            finally:
                window.close()
        # 切替が切なら送らない。
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            fills2: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender2(text, profile, attachments=None):
                fills2.append(text)
                return sr._AiAnswerResult(True, "入れました")

            window.ai_sender = fake_sender2  # type: ignore
            window.ai_presser = (  # type: ignore[method-assign]
                lambda profile: sr._AiAnswerResult(True, "押しました"))
            try:
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, history_marker={"id": "h-9"}))
                settle()
                assert fills2 == []
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_log_auto_response():
    # 動作ログリクエストの自動応答 (1.96):
    # - 切替が入っていて目印 (指定つき) が来たら、動作ログ
    #   から指定どおりに選んだ分だけを送り、消費が控えに
    #   残ること。同じ目印では二度送らないこと。
    # - 切替が切なら何も送らないこと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_log=True,
                ai_target={"process_name": "dummy.exe"})
            core.save_log_events([
                {"at": 1000.0 + i * 60.0, "text": f"ログ{i + 1}"}
                for i in range(5)])
            fills: list = []
            presses: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender(text, profile, attachments=None):
                fills.append((text, list(attachments or [])))
                return sr._AiAnswerResult(True, "入れました")

            def fake_presser(profile):
                presses.append(profile)
                return sr._AiAnswerResult(True, "押しました")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                assert window._automation_flags()[
                    "auto_log"] is True
                # 間隔の歯止めはテストでは外す。
                window._log_auto["last_sent_at"] = 0.0
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    log_marker={"id": "lg-1", "params": "2"}))
                assert pump_until(lambda: len(presses) == 1)
                sent_lines = fills[0][0].splitlines()
                assert sent_lines[0] == "動作ログ（直近2件）"
                assert len(sent_lines) == 3
                assert "ログ4" in sent_lines[1]
                assert "ログ5" in sent_lines[2]
                assert fills[0][1] == []
                assert pump_until(
                    lambda: core.load_log_auto()[
                        "consumed_id"] == "lg-1")
                # 同じ目印では二度送らない。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None,
                    log_marker={"id": "lg-1", "params": "2"}))
                settle()
                assert len(fills) == 1
            finally:
                window.close()
        # 切替が切なら送らない。
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            fills2: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender2(text, profile, attachments=None):
                fills2.append(text)
                return sr._AiAnswerResult(True, "入れました")

            window.ai_sender = fake_sender2  # type: ignore
            window.ai_presser = (  # type: ignore[method-assign]
                lambda profile: sr._AiAnswerResult(True, "押しました"))
            try:
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, log_marker={"id": "lg-9"}))
                settle()
                assert fills2 == []
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_stop_auto_response():
    # 停止リクエストの自動応答 (1.93):
    # (a) 実行中なら、合図で実行が止まり (偽ハンドルの stop が
    #   呼ばれる)、消費が控えに残ること。別途の送信はしない
    #   こと (報告は止まった実行の結果文が担う)。
    # (b) 実行中でなければ「実行中のスクリプトはありません。」
    #   が送られ、消費が残ること。同じ目印では二度送らない。
    # (c) 切替が切なら、実行中でも止めないこと。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")

    def settle(seconds=0.5):
        pump_until(lambda: False, timeout=seconds)

    class _FakeHandle:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class _FakeItem:
        def __init__(self, name):
            self.name = name

    try:
        # --- (a) 実行中は止める ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_stop=True,
                ai_target={"process_name": "dummy.exe"})
            fills: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender(text, profile, attachments=None):
                fills.append(text)
                return sr._AiAnswerResult(True, "入れました")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = (  # type: ignore[method-assign]
                lambda profile: sr._AiAnswerResult(True, "押しました"))
            try:
                assert window._automation_flags()[
                    "auto_stop"] is True
                # 間隔の歯止めはテストでは外す。
                window._stop_auto["last_sent_at"] = 0.0
                handle = _FakeHandle()
                window._run_handle = handle
                window._running_item = _FakeItem("1-999_long.phal")
                window._maybe_auto_stop({"id": "s-1", "key": "k-1"})
                assert handle.stopped is True
                assert core.load_stop_auto()[
                    "consumed_id"] == "s-1"
                settle()
                assert fills == []
                window._run_handle = None
                window._running_item = None
            finally:
                window.close()
        # --- (b) 実行中でなければ文で返す ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, auto_stop=True,
                ai_target={"process_name": "dummy.exe"})
            fills = []
            presses: list = []
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_sender_b(text, profile, attachments=None):
                fills.append((text, list(attachments or [])))
                return sr._AiAnswerResult(True, "入れました")

            def fake_presser_b(profile):
                presses.append(profile)
                return sr._AiAnswerResult(True, "押しました")

            window.ai_sender = fake_sender_b  # type: ignore
            window.ai_presser = fake_presser_b  # type: ignore
            try:
                window._stop_auto["last_sent_at"] = 0.0
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, stop_marker={"id": "s-2"}))
                assert pump_until(lambda: len(presses) == 1)
                assert fills[0][0] == "実行中のスクリプトはありません。"
                assert fills[0][1] == []
                assert pump_until(
                    lambda: core.load_stop_auto()[
                        "consumed_id"] == "s-2")
                # 同じ目印では二度送らない。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, stop_marker={"id": "s-2"}))
                settle()
                assert len(fills) == 1
            finally:
                window.close()
        # --- (c) 切替が切なら止めない ---
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: None)
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))
            try:
                assert window._automation_flags()[
                    "auto_stop"] is False
                handle = _FakeHandle()
                window._run_handle = handle
                window._running_item = _FakeItem("1-999_long.phal")
                window._maybe_auto_stop({"id": "s-9"})
                assert handle.stopped is False
                window._run_handle = None
                window._running_item = None
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_diag_writes_log_and_collapses_cached_checks():
    # 動作ログ (1.89): _diag は診断の控えと動作ログの両方へ
    # 書く。「チェック（控えから）」は個別に入らず、畳むと
    # 1行 (件数) になること。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, _watch = _make_dl_window(tmp)
            window = sr.ScriptRunnerWindow(str(settings_path))
            try:
                window._diag("通常の記録です")
                window._diag("チェック（控えから）: a.phal → OK")
                window._diag("チェック（控えから）: b.phal → 警告")
                texts = [e["text"] for e in core.load_log_events()]
                assert texts == ["通常の記録です"]
                assert window._cached_check_count == 2
                window._flush_cached_check_log()
                texts = [e["text"] for e in core.load_log_events()]
                assert len(texts) == 2
                assert "2件" in texts[1]
                assert window._cached_check_count == 0
                # 診断の控えには個別の行が残る (束の従来どおり)。
                diag_texts = [
                    e["text"] for e in core.load_diag_events()]
                assert any("a.phal" in t for t in diag_texts)
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_log_viewer_dialog_shows_events():
    # 動作ログビューア (1.90): 置き場の記録が日付つきで出る
    # ことと、設定画面に「動作ログ」ボタンがあること。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            events = []
            core.log_append(events, "ビューア確認の一行", at=1000.0)
            core.save_log_events(events)
            viewer = sr_ai.LogViewerDialog()
            try:
                assert "ビューア確認の一行" in \
                    viewer._view.toPlainText()
            finally:
                viewer.close()
            dialog = sr_ai.SettingsDialog(
                {"process_name": "dummy.exe"}, None)
            try:
                labels = [dialog._tabs.tabText(i)
                          for i in range(dialog._tabs.count())]
                assert "動作ログ" in labels
                assert "ツール設定" in labels
            finally:
                dialog.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_ai_window_attach_method_and_test_button():
    # 添付方式 (1.61):
    # - プロファイルの target に attach_method が保存され、
    #   sanitize で不正値が auto に倒れる。build_profile にも渡る。
    # - 設定画面では選択で切り替えられ、「添付テスト」は今の
    #   プロファイル (保存前でも現在の入力値) でテスト役を呼び、
    #   結果が状態ラベルに出る。テスト用ファイルは outbox に
    #   書かれる。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
        try:
            assert sr_ai.sanitize_attach_method("dialog") == "dialog"
            assert sr_ai.sanitize_attach_method("変な値") == "auto"
            assert sr_ai.sanitize_attach_method(None) == "auto"
            profiles, active = sr_ai.sanitize_profiles({
                "ai_profiles": [
                    {"name": "A", "target": {
                        "process_name": "dummy.exe",
                        "attach_method": "dialog"}},
                    {"name": "B", "target": {
                        "process_name": "dummy.exe",
                        "attach_method": "bogus"}},
                ],
                "ai_profile_active": 0,
            })
            assert profiles[0]["target"]["attach_method"] == "dialog"
            assert profiles[1]["target"]["attach_method"] == "auto"
            built = sr_ai.build_profile(profiles[0]["target"])
            assert built["attach_method"] == "dialog"
            # ZIP の名前変え (1.81) も build_profile へ渡る。
            # sanitize_fields を通る前に取り出すのが肝 (1.84)。
            zprofiles, _zactive = sr_ai.sanitize_profiles({
                "ai_profiles": [{"name": "Z", "target": {
                    "process_name": "dummy.exe",
                    "zip_as_txt": True}}],
                "ai_profile_active": 0,
            })
            assert zprofiles[0]["target"]["zip_as_txt"] is True
            assert sr_ai.build_profile(
                zprofiles[0]["target"])["zip_as_txt"] is True
            saved = sr_ai.profiles_settings(profiles, active)
            assert saved["ai_profiles"][0]["target"][
                "attach_method"] == "dialog"

            tested = []

            def fake_tester(path, profile):
                tested.append((path, profile))
                return sr._AiAnswerResult(
                    True, "添付できました（方式: ダイアログ）。"
                          "チップも外しました")

            dialog = sr_ai.SettingsDialog(
                profiles[active]["target"], None,
                profiles=profiles, active_index=active,
                attach_tester=fake_tester)
            try:
                page = dialog.ai_page
                # 選択は保存済みの方式を指している。
                assert page._attach_method_combo.currentData() \
                    == "dialog"
                assert page.fields()["attach_method"] == "dialog"
                page._attach_test_button.click()
                assert pump_until(lambda: len(tested) == 1)
                path, profile = tested[0]
                assert path.endswith("phal-attach-test.txt")
                assert profile["attach_method"] == "dialog"
                # テスト用ファイルが outbox に書かれている。
                assert (core.outbox_dir()
                        / "phal-attach-test.txt").exists()
                assert pump_until(
                    lambda: "添付できました"
                    in page._status_label.text())
                # 切り替えると fields にも反映される。
                page._attach_method_combo.setCurrentIndex(
                    page._attach_method_combo.findData("paste"))
                assert page.fields()["attach_method"] == "paste"
            finally:
                dialog.close()
        finally:
            if prev_data_dir is None:
                os.environ.pop("TRAYTOOLS_DATA_DIR", None)
            else:
                os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


_DUAL_PROFILES = [
    {"name": "Muse",
     "target": {"process_name": "claude.exe", "label": "Muse"}},
    {"name": "Copilot",
     "target": {"process_name": "copilot.exe", "label": "Copilot"}},
]


def _make_dual_window(tmp: Path, **extra):
    """二重待受の設定 (主 Muse / 副 Copilot) で窓の下ごしらえ。"""
    section = {
        "ai_profiles": _DUAL_PROFILES,
        "ai_profile_active": 0,
        "ai_dual_watch": True,
        "ai_secondary_profile": "Copilot",
    }
    section.update(extra)
    return _make_dl_window(tmp, **section)


def _origin_item(watch: Path, ai_name: str):
    """出どころを付けた、回答できる状態の項目を1つ作る。"""
    path = write_script(watch, "9-6_origin.phal",
                        "#!phal python\n# 9-6: 出どころのテスト\n"
                        "print(1)\n")
    stat = path.stat()
    item = sr.ScriptItem(path, stat.st_mtime, stat.st_size)
    item.ai = ai_name
    item.last_result_text = "結果文"
    item.last_run = object()  # 回答側は有無だけを見る
    return item


def test_dl_candidate_suppressed_by_id_and_ai():
    # DL 済みの物と AI 名・コマンドID が一致する候補は、
    # 特別行に出さない (2.25、Nagato の提案)。概要の ID
    # 接頭辞にコロンが無い形 (実機の設置ファイルの形) でも
    # 当たる。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp, ai_target={"process_name": "dummy.exe"})
            write_script(
                watch, "9-9_install_phal.phal",
                "#!phal python\n# 9-9 出さないスクリプトです\n"
                "print('中身')\n")
            window = sr.ScriptRunnerWindow(str(settings_path))
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))
            try:
                window._poll()
                settle_checks(window, ["9-9_install_phal.phal"])
                state = window._watch_list[0]
                item = next(
                    i for i in window._items
                    if i.name == "9-9_install_phal.phal")
                item.ai = state.name  # DL の出どころを再現
                cand = {
                    "name": "#!phal python\n"
                            "# 9-9 出さないスクリプトです\n"
                            "WATCHDOG = 'x'\n",
                    "class_name": "cls group/card",
                    "label": "9-9 出さないスクリプトです",
                    "summary": "9-9 出さないスクリプトです",
                }
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", cand))
                QCoreApplication.processEvents()
                assert state.holder is None
                scans = [e["text"] for e in window._diag_events
                         if e["text"].startswith("直近の走査:")]
                assert any("ID・AI名一致" in s for s in scans), \
                    scans
                # ID が違えば出る。
                other = dict(cand)
                other["name"] = cand["name"].replace("9-9", "9-10")
                other["summary"] = "9-10 別のスクリプトです"
                other["label"] = other["summary"]
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", other))
                QCoreApplication.processEvents()
                assert state.holder is not None
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_settings_helpers():
    # 二重待受の設定の整えと、設定確認の報告 (2.08)。
    import script_runner_ai as sr_ai

    assert sr_ai.sanitize_dual_watch(
        {"ai_dual_watch": True,
         "ai_secondary_profile": " Copilot "}) == \
        {"enabled": True, "secondary": "Copilot"}
    assert sr_ai.sanitize_dual_watch({}) == \
        {"enabled": False, "secondary": ""}
    assert sr_ai.sanitize_dual_watch(
        {"ai_dual_watch": "yes"})["enabled"] is False
    report = core.build_settings_report(
        {"ai_dual_watch": True, "ai_secondary_profile": "Copilot"},
        "2.08")
    assert "二重待受: 入（副: Copilot）" in report, report
    report_off = core.build_settings_report({}, "2.08")
    assert "二重待受: 切" in report_off, report_off
    # 見張る窓の集合 (2.20): 新キーがあればそれが正、無ければ
    # 旧キー (アクティブ + 二重待受の副) から導く。並びは
    # プロファイル一覧の順で、一覧に無い名前は落ちる。
    profiles = [
        {"name": "Muse", "target": {}},
        {"name": "Copilot", "target": {}},
        {"name": "Third", "target": {}},
    ]
    base = {"ai_profiles": profiles, "ai_profile_active": 0}
    assert sr_ai.sanitize_watch_profiles(base) == ["Muse"]
    assert sr_ai.sanitize_watch_profiles(
        {**base, "ai_dual_watch": True,
         "ai_secondary_profile": "Copilot"}) == ["Muse", "Copilot"]
    assert sr_ai.sanitize_watch_profiles(
        {**base, "ai_dual_watch": True}) == ["Muse", "Copilot"]
    assert sr_ai.sanitize_watch_profiles(
        {**base, "ai_watch_profiles": ["Third", "Muse", "幽霊"]}
    ) == ["Muse", "Third"]
    assert sr_ai.sanitize_watch_profiles(
        {**base, "ai_watch_profiles": []}) == []
    report_set = core.build_settings_report(
        {**base, "ai_watch_profiles": ["Muse", "Copilot"]}, "2.20")
    assert "見張る窓: Muse、Copilot" in report_set, report_set


def test_dual_watch_second_window_candidate():
    # 二重待受 (2.08): 副の窓の候補は副の状態に乗り、自動の押下は
    # 副のプロファイルで押す。主の状態には乗らない。到着待ちには
    # 副の窓の名前が残る (到着したファイルの出どころになる)。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(tmp, auto_dl=True)
            window = sr.ScriptRunnerWindow(str(settings_path))
            window._set_clipboard = (  # type: ignore[method-assign]
                lambda text: setattr(window, "_last_clipboard_text",
                                     text))
            presses: list = []
            window.ai_downloader = (  # type: ignore[method-assign]
                lambda profile: sr._DlCheckResult(False, "なし", None))

            def fake_presser(profile, key=None, _p=presses):
                _p.append(profile)
                return sr._AiAnswerResult(True, "ok")

            window.ai_dl_presser = fake_presser  # type: ignore
            try:
                states = window._sync_watch_states()
                assert [s.name for s in states] == \
                    ["Muse", "Copilot"], [s.name for s in states]
                assert states[0].profile["process_name"] == \
                    "claude.exe"
                assert states[1].profile["process_name"] == \
                    "copilot.exe"
                # 最初の走査 (候補なし) で起動時分の控えを確定する。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, watch="Copilot"))
                candidate = {
                    "name": "#!phal python\n# 9-5: 副の窓のテスト\n"
                            "print(1)\n",
                    "class_name": "cls group/card",
                    "label": "9-5: 副の窓のテスト",
                    "summary": "9-5: 副の窓のテスト",
                }
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", candidate, watch="Copilot"))
                assert window._watch_list[1].holder is not None
                assert window._watch_list[0].holder is None
                # 特別行は1行で、AI の列に副の名前が出る。
                assert window.table.rowCount() == 1
                assert window.table.item(0, sr.COL_AI).text() == \
                    "Copilot"
                # 自動の押下は副のプロファイルで押される。
                assert pump_until(lambda: len(presses) == 1), presses
                assert presses[0]["process_name"] == "copilot.exe", \
                    presses
                assert pump_until(
                    lambda: window._watch_list[1].holder is None
                    and not window._dl_pressing)
                assert window._dl_awaiting is not None
                assert window._dl_awaiting[2] == "Copilot"
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_answer_routes_to_origin():
    # 回答は項目の出どころの窓へ入る (2.08)。出どころが無ければ
    # 有効なプロファイル (主) へ入る。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(tmp)
            window = sr.ScriptRunnerWindow(str(settings_path))
            sent: list = []

            def fake_sender(text, profile, attachments=None):
                sent.append((text, profile))
                return sr._AiAnswerResult(True, "ok")

            window.ai_sender = fake_sender  # type: ignore
            try:
                configured, profile = window._resolve_item_target(
                    _origin_item(watch, "Copilot"))
                assert configured
                assert profile["process_name"] == "copilot.exe"
                window._answer_to_ai(_origin_item(watch, "Copilot"))
                assert pump_until(lambda: len(sent) == 1), sent
                assert sent[0][1]["process_name"] == "copilot.exe"
                window._answer_to_ai(_origin_item(watch, ""))
                assert pump_until(lambda: len(sent) == 2), sent
                assert sent[1][1]["process_name"] == "claude.exe"
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_off_stays_single():
    # 二重待受が切なら、見張る窓は主の1つだけ (従来どおり、2.08)。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp,
                ai_profiles=_DUAL_PROFILES,
                ai_profile_active=0)
            window = sr.ScriptRunnerWindow(str(settings_path))
            try:
                states = window._sync_watch_states()
                assert [s.name for s in states] == ["Muse"], \
                    [s.name for s in states]
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_floor_checks_toggle():
    # 階のチェック (2.14、置き場は 2.15 で大きな進捗表示の階へ、
    # 集合化は 2.20): どの階のチェックも操作でき、付け外しが
    # 見張る窓の集合 (ai_watch_profiles) として設定へ保存され
    # る。主のチェックも外せる (見張りゼロも可)。階のチェック
    # 表示も追随する。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp,
                ai_profiles=_DUAL_PROFILES,
                ai_profile_active=0)
            window = sr.ScriptRunnerWindow(str(settings_path))
            try:
                boxes = {name: floor["check"]
                         for name, floor in window._floors.items()}
                assert boxes["Muse"].isChecked()
                assert boxes["Muse"].isEnabled()
                assert not boxes["Copilot"].isChecked()
                window._on_ai_check_toggled("Copilot", True)
                saved = json.loads(
                    settings_path.read_text(encoding="utf-8")
                )["script_runner"]
                assert saved["ai_watch_profiles"] == [
                    "Muse", "Copilot"]
                assert window._floors["Copilot"]["check"].isChecked()
                window._on_ai_check_toggled("Copilot", False)
                saved = json.loads(
                    settings_path.read_text(encoding="utf-8")
                )["script_runner"]
                assert saved["ai_watch_profiles"] == ["Muse"]
                assert not window._floors[
                    "Copilot"]["check"].isChecked()
                # 主も外せる (見張りゼロ)。
                window._on_ai_check_toggled("Muse", False)
                saved = json.loads(
                    settings_path.read_text(encoding="utf-8")
                )["script_runner"]
                assert saved["ai_watch_profiles"] == []
                assert not window._floors["Muse"]["check"].isChecked()
                window._sync_watch_states()
                assert window._watch_list == []
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_floors_grouped_aligned_and_pulse_per_floor():
    # 階の見た目 (2.16): 階ごとに枠 (QFrame) で分かれ、名前の
    # 欄の幅は階どうしで揃う。明滅は監視している側の階だけ
    # (副は見張りが割り当たってから、非常停止中はどこも
    # 明滅しない)。
    from PySide6.QtWidgets import QFrame
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(
                tmp,
                ai_profiles=_DUAL_PROFILES,
                ai_profile_active=0)
            window = sr.ScriptRunnerWindow(str(settings_path))
            try:
                floors = window._floors
                assert all(isinstance(f["row"], QFrame)
                           for f in floors.values())
                widths = {f["check"].width()
                          for f in floors.values()}
                assert len(widths) == 1, widths
                assert not window._floor_pulsing("Copilot")
                window._on_ai_check_toggled("Copilot", True)
                window._sync_watch_states()
                state = next(
                    s for s in window._watch_list
                    if s.name == "Copilot")
                assert window._floor_pulsing("Copilot") == (
                    state.profile is not None)
                # 明滅は背景の層だけ (2.25): 行自体に
                # エフェクトは付かず、背景の層に付く。
                assert floors["Copilot"]["row"].graphicsEffect() \
                    is None
                assert floors["Copilot"]["bg"].graphicsEffect() \
                    is not None
                # 待機の起点が立っていなくても、階の描き直しで
                # 起点が立ってカウントアップが始まる (2.24)。
                state.idle_since = None
                window._render_floor_idle(
                    "Copilot", floors["Copilot"])
                assert state.idle_since is not None
                assert floors["Copilot"]["number"].text() != "―"
                # 見張りを外した階は明滅しない (2.23、実機報告)。
                # 既定の階 (Muse) も同じ判定。
                window._on_ai_check_toggled("Muse", False)
                window._sync_watch_states()
                assert not window._floor_pulsing("Muse")
                assert window._floor_pulsing("Copilot") == (
                    state.profile is not None)
                window._emergency_stopped = True
                assert not window._floor_pulsing("Copilot")
                assert not window._floor_pulsing("Muse")
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_single_profile_floor_has_no_check():
    # プロファイルが1つだけなら、階は1本だけでチェックは
    # 出ない (2.15)。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dl_window(tmp)
            window = sr.ScriptRunnerWindow(str(settings_path))
            try:
                assert len(window._floors) == 1
                floor = next(iter(window._floors.values()))
                assert floor["check"] is None
                assert floor["name_label"] is not None
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_relay_to_secondary():
    # 中継リクエスト (2.10、経路は 2.17): 引数が無い目印は、
    # 見つかった窓の「もう一方」のプロファイルへ送られる。
    # 同じ合図の再検出では送り直さない。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(
                tmp, auto_relay=True)
            window = sr.ScriptRunnerWindow(str(settings_path))
            sent: list = []
            pressed: list = []

            def fake_sender(text, profile, attachments=None):
                sent.append((text, profile))
                return sr._AiAnswerResult(True, "ok")

            def fake_presser(profile):
                pressed.append(profile)
                return sr._AiAnswerResult(True, "ok")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                window._sync_watch_states()
                marker = {"id": "relay-1", "key": "rk-1",
                          "body": "副への文\n2行目"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker,
                    watch="Muse"))
                assert pump_until(lambda: len(sent) >= 1), sent
                assert sent[0][0] == "副への文\n2行目", sent
                assert sent[0][1]["process_name"] == "copilot.exe", \
                    sent
                assert pump_until(lambda: len(pressed) >= 1), pressed
                assert pressed[0]["process_name"] == "copilot.exe"
                # 成否の返事 (ACK) が主のプロファイルへ送られる
                # (2.12)。
                assert pump_until(lambda: len(sent) >= 2), sent
                assert "中継しました" in sent[1][0], sent
                assert sent[1][1]["process_name"] != "copilot.exe", \
                    sent
                # 同じ合図の再検出では送り直さない (消費済み)。
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker,
                    watch="Muse"))
                assert not pump_until(
                    lambda: len(sent) == 3, timeout=1.0), sent
                # 副の窓からの合図も、引数が無ければ「もう一方」
                # へ中継する (2.17、主副の区別なし)。間隔の歯止め
                # は、時刻を進めたことにして外す。
                window._relay_auto["last_sent_at"] = 0.0
                marker2 = {"id": "relay-2", "key": "rk-2",
                           "body": "逆向きの文"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker2,
                    watch="Copilot"))
                assert pump_until(lambda: len(sent) >= 3), sent
                assert sent[2][0] == "逆向きの文", sent
                assert sent[2][1]["process_name"] != "copilot.exe", \
                    sent
                # ACK は送り元 (Copilot) へ返る (2.17)。
                assert pump_until(lambda: len(sent) >= 4), sent
                assert "中継しました" in sent[3][0], sent
                assert sent[3][1]["process_name"] == "copilot.exe", \
                    sent
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_relay_route_arg():
    # 中継の引数 (2.17): 「送り元2送り先」で経路が決まる。
    # 送り元の窓で見つかったときだけ発火し、ACK は送り元へ
    # 返る。送り元でない窓で見つけた目印は何も送らない。
    # ほどけない引数は、送らずに理由だけ送り元へ返す。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(
                tmp, auto_relay=True)
            window = sr.ScriptRunnerWindow(str(settings_path))
            sent: list = []

            def fake_sender(text, profile, attachments=None):
                sent.append((text, profile))
                return sr._AiAnswerResult(True, "ok")

            def fake_presser(profile):
                return sr._AiAnswerResult(True, "ok")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                window._sync_watch_states()
                # Copilot から Muse へ (引数つき)。
                marker = {"id": "relay-a1", "key": "rk-a1",
                          "body": "引数つきの文",
                          "arg": "Copilot2Muse"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker,
                    watch="Copilot"))
                assert pump_until(lambda: len(sent) >= 1), sent
                assert sent[0][0] == "引数つきの文", sent
                assert sent[0][1]["process_name"] != "copilot.exe", \
                    sent
                assert pump_until(lambda: len(sent) >= 2), sent
                assert "中継しました" in sent[1][0], sent
                assert sent[1][1]["process_name"] == "copilot.exe", \
                    sent
                # 送り元でない窓で見つけた目印は、何も送らない。
                marker2 = {"id": "relay-a2", "key": "rk-a2",
                           "body": "送られない文",
                           "arg": "Muse2Copilot"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker2,
                    watch="Copilot"))
                assert not pump_until(
                    lambda: len(sent) > 2, timeout=1.0), sent
                # ほどけない引数は送らず、理由が送り元へ返る。
                marker3 = {"id": "relay-a3", "key": "rk-a3",
                           "body": "宛先不明の文",
                           "arg": "Muse2Nowhere"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker3,
                    watch="Muse"))
                assert pump_until(lambda: len(sent) == 3), sent
                assert "引数が分かりません" in sent[2][0], sent
                assert sent[2][1]["process_name"] != "copilot.exe", \
                    sent
                assert all("宛先不明の文" not in text
                           for text, _profile in sent), sent
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_relay_flag_off():
    # 中継の切替が切なら、本文は中継しない (2.10)。見送りの
    # NACK だけが主へ返る (2.12、NACK の形は 2.19)。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(tmp)
            window = sr.ScriptRunnerWindow(str(settings_path))
            sent: list = []

            def fake_sender(text, profile, attachments=None):
                sent.append((text, profile))
                return sr._AiAnswerResult(True, "ok")

            def fake_presser(profile):
                return sr._AiAnswerResult(True, "ok")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                window._sync_watch_states()
                marker = {"id": "relay-9", "key": "rk-9",
                          "body": "送られない文"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker,
                    watch="Muse"))
                assert pump_until(lambda: len(sent) == 1), sent
                assert "中継NACK" in sent[0][0], sent
                assert sent[0][1]["process_name"] != "copilot.exe", \
                    sent
                assert all("送られない文" not in text
                           for text, _profile in sent), sent
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_relay_empty_body_nack():
    # 本文が無い目印は送らず、理由が NACK で送り元へ返る
    # (2.19)。黙って見送ると、送り元は形式ミスに気づけない。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(
                tmp, auto_relay=True)
            window = sr.ScriptRunnerWindow(str(settings_path))
            sent: list = []

            def fake_sender(text, profile, attachments=None):
                sent.append((text, profile))
                return sr._AiAnswerResult(True, "ok")

            def fake_presser(profile):
                return sr._AiAnswerResult(True, "ok")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                window._sync_watch_states()
                marker = {"id": "relay-e1", "key": "rk-e1",
                          "body": "", "arg": "Muse2Copilot"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker,
                    watch="Muse"))
                assert pump_until(lambda: len(sent) == 1), sent
                assert "中継NACK" in sent[0][0], sent
                assert "本文がありません" in sent[0][0], sent
                assert sent[0][1]["process_name"] != "copilot.exe", \
                    sent
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_relay_pending_fires_from_stash():
    # 中継の保留控え (2.13): 連鎖が詰まって見送られた中継は
    # 本文ごと控えに残り、目印が走査から消えた後でも、連鎖が
    # 空いた走査で控えから送られる。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(
                tmp, auto_relay=True)
            window = sr.ScriptRunnerWindow(str(settings_path))
            sent: list = []
            pressed: list = []

            def fake_sender(text, profile, attachments=None):
                sent.append((text, profile))
                return sr._AiAnswerResult(True, "ok")

            def fake_presser(profile):
                pressed.append(profile)
                return sr._AiAnswerResult(True, "ok")

            window.ai_sender = fake_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                window._sync_watch_states()
                marker = {"id": "relay-p1", "key": "rk-p1",
                          "body": "保留された文"}
                # 連鎖が詰まっている間は送らず、控えに残る。
                window._ai_sending = True
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker,
                    watch="Muse"))
                assert not pump_until(
                    lambda: len(sent) >= 1, timeout=1.0), sent
                pending = core.sanitize_relay_pending(
                    core.load_relay_auto().get("pending"))
                assert pending is not None, core.load_relay_auto()
                assert pending["id"] == "relay-p1"
                assert pending["body"] == "保留された文"
                # 連鎖が空いて、目印の無い走査が来たら控えから
                # 送る。
                window._ai_sending = False
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=None,
                    watch="Muse"))
                assert pump_until(lambda: len(sent) >= 1), sent
                assert sent[0][0] == "保留された文", sent
                assert sent[0][1]["process_name"] == "copilot.exe"
                assert pump_until(lambda: len(pressed) >= 1)
                # 成功で消費され、控えも消える。ACK も主へ返る。
                assert pump_until(lambda: len(sent) >= 2), sent
                assert "中継しました" in sent[1][0], sent
                state = core.load_relay_auto()
                assert state["consumed_id"] == "relay-p1", state
                assert state["pending"] is None, state
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_dual_watch_relay_retry_once_on_failure():
    # 中継の送り直し (2.13): 送信に失敗したら消費せず、少し
    # 置いて1回だけ送り直す。2回目も失敗したら消費して
    # 終わる。書きかけ (needs_confirm) では送り直さない。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(
                tmp, auto_relay=True)
            window = sr.ScriptRunnerWindow(str(settings_path))
            attempts: list = []

            def failing_sender(text, profile, attachments=None):
                attempts.append((text, profile))
                if profile["process_name"] == "copilot.exe":
                    return sr._AiAnswerResult(
                        False, "入力欄に入れられませんでした")
                return sr._AiAnswerResult(True, "ok")

            def fake_presser(profile):
                return sr._AiAnswerResult(True, "ok")

            window.ai_sender = failing_sender  # type: ignore
            window.ai_presser = fake_presser  # type: ignore
            try:
                window._sync_watch_states()
                marker = {"id": "relay-r1", "key": "rk-r1",
                          "body": "届けたい文"}
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=marker,
                    watch="Muse"))
                # 1回目が失敗し、送り直しの ACK が主へ返る。
                # 消費はされない。
                assert pump_until(lambda: len(attempts) == 2), \
                    attempts
                assert attempts[0][0] == "届けたい文", attempts
                assert "送り直します" in attempts[1][0], attempts
                state = core.load_relay_auto()
                assert state["consumed_id"] == "", state
                pending = core.sanitize_relay_pending(
                    state.get("pending"))
                assert pending is not None, state
                assert pending["retries"] == 1, pending
                # 待ち時間を過ぎたことにして、目印の無い走査で
                # 送り直しを走らせる。
                window._relay_auto["pending"]["not_before"] = 0.0
                window._on_dl_checked(sr._DlCheckResult(
                    True, "", None, relay_marker=None,
                    watch="Muse"))
                assert pump_until(lambda: len(attempts) == 4), \
                    attempts
                assert attempts[2][0] == "届けたい文", attempts
                assert "中継に失敗しました" in attempts[3][0], \
                    attempts
                assert "送り直します" not in attempts[3][0], \
                    attempts
                state = core.load_relay_auto()
                assert state["consumed_id"] == "relay-r1", state
                assert state["pending"] is None, state
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


def test_status_qr_text_and_encoding():
    # 状態QR (2.09): 窓の状態から中身が組め、見張る窓の設定が
    # 反映され、実際に QR へ符号化できる。
    prev_data_dir = os.environ.get("TRAYTOOLS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            os.environ["TRAYTOOLS_DATA_DIR"] = str(tmp / "data")
            settings_path, watch = _make_dual_window(tmp)
            window = sr.ScriptRunnerWindow(str(settings_path))
            try:
                text = window._status_qr_text()
                assert f"PHAL状態 Ver{sr.PHAL_VERSION}" in text, text
                assert "見張る窓 ☑Muse ☑Copilot" in text, text
                assert "連携先" not in text, text
                assert "状態 待機中（段階 なし）" in text, text
                assert "手数 0" in text, text
                import diagnostics

                qr = diagnostics.make_qr(text)
                assert len(qr.matrix) > 0
                image = diagnostics.qr_to_qimage(qr, scale=2)
                assert image.width() == image.height() > 0
            finally:
                window.close()
    finally:
        if prev_data_dir is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = prev_data_dir


if __name__ == "__main__":
    tests = [
        test_window_lists_and_checks,
        test_f5_gating_and_run_copies_result,
        test_phal_status_published_on_run,
        test_table_columns_and_double_click_and_id_header,
        test_error_copy_text,
        test_overwrite_rechecks_and_clears_ran,
        test_run_aborts_when_file_changed_after_check,
        test_window_settings_saved,
        test_window_startup_cleans_bom_leftovers,
        test_window_width_with_long_folder_path,
        test_restart_command_arranges_helper_and_closes,
        test_entry_wiring_strings,
        test_hide_no_id_filter,
        test_return_and_trash_actions,
        test_run_button_press_and_reuse,
        test_poll_problem_reported_and_others_listed,
        test_auto_run_only_after_launch_files,
        test_loop_survives_restart,
        test_dl_loop_brakes,
        test_status_verdict_and_ran_survives_restart,
        test_diag_events_recorded_and_bundle_button,
        test_escape_sent_after_dl_arrival,
        test_diag_auto_response,
        test_reset_auto_response,
        test_blocked_notice,
        test_dark_mode_toggle_and_startup,
        test_intro_button_falls_back_when_unconfigured,
        test_intro_button_cancelled_folder_dialog,
        test_intro_auto_text_zip_line_follows_profile,
        test_record_button_copies_loop_record,
        test_rerun_menu_and_diff,
        test_answer_with_attachments,
        test_ai_window_attach_method_and_test_button,
        test_dual_watch_settings_helpers,
        test_dual_watch_second_window_candidate,
        test_dual_watch_answer_routes_to_origin,
        test_dual_watch_off_stays_single,
        test_status_qr_text_and_encoding,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
