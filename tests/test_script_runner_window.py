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
            assert "クリップボードに入れました" in window.note.text()
            # Ctrl+C で直前の結果をもう一度入れる。
            copied.clear()
            window._copy_last()
            assert copied == [text]
        finally:
            window.close()


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
            window.close()
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            section = saved["script_runner"]
            assert section["window_geometry"] == [123, 45, 360, 555], section
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
                write_script(watch, "clean.py", "print('新しい分')\n")
                write_script(watch, "warn.py",
                             "import os\nos.remove('x')\n")
                write_script(watch, "broken.py", "def broken(:\n")
                window._poll()
                window._poll()
                settle_checks(window,
                              ["clean.py", "warn.py", "broken.py"])
                assert pump_until(lambda: len(calls) == 2), calls
                assert sorted(calls) == ["clean.py", "warn.py"], calls
                assert active["peak"] == 1, active
                broken = item_of(window, "broken.py")
                assert broken is not None and not broken.ran
                assert broken.last_run is None
                clean = item_of(window, "clean.py")
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
                write_script(watch, "late.py", "print('切替なし')\n")
                window._poll()
                window._poll()
                settle_checks(window, ["late.py"])
                assert pump_until(lambda: False, timeout=0.6) is False
                late = item_of(window, "late.py")
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
    #     自動で走ること。
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
                write_script(watch, "fresh.py", "print('新しい分')\n")
                window_a._poll()
                window_a._poll()
                settle_checks(window_a, ["fresh.py"])
                fresh_a = item_of(window_a, "fresh.py")
                assert fresh_a is not None
                assert fresh_a.auto_eligible is True
                assert not fresh_a.ran and calls == [], calls
                records = core.load_pending_runs()
                assert str(watch / "fresh.py") in records, records
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
                settle_checks(window_b, ["backlog.py", "fresh.py"])
                assert pump_until(lambda: calls == ["fresh.py"]), calls
                assert pump_until(lambda: False, timeout=0.6) is False
                assert calls == ["fresh.py"], calls
                backlog_b = item_of(window_b, "backlog.py")
                assert backlog_b is not None and not backlog_b.ran
                assert backlog_b.auto_eligible is False
                fresh_b = item_of(window_b, "fresh.py")
                assert fresh_b is not None and fresh_b.ran
                assert fresh_b.auto_eligible is True
                records = core.load_pending_runs()
                assert str(watch / "fresh.py") not in records, records
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

            def fake_presser(profile, _p=presses):
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
                assert window._dl_holder is None
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
                    lambda: window._dl_holder is None
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
                lambda profile: sr._AiAnswerResult(True, "ok"))
            try:
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate))
                assert window._dl_holder is not None
                window._press_download(auto=False)
                assert pump_until(
                    lambda: not window._dl_pressing
                    and window._dl_holder is None)
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
                assert window._dl_holder is None
                # ファイルが消えたら、落とし直せるよう再び出る。
                (watch / "9-2_install_phal.phal").unlink()
                window._poll()
                assert pump_until(
                    lambda: item_of(window,
                                    "9-2_install_phal.phal") is None)
                window._on_dl_checked(
                    sr._DlCheckResult(True, "", candidate))
                QCoreApplication.processEvents()
                assert window._dl_holder is not None
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
                    write_script(watch, "orig.py", content)
                    window._poll()
                    window._poll()
                    settle_checks(window, ["orig.py"])
                    assert pump_until(
                        lambda: calls == ["orig.py"]), calls
                    assert pump_until(
                        lambda: window._running_item is None)
                    sha = core.file_sha256(watch / "orig.py")
                    assert core.executed_hash_contains(
                        core.load_executed_hashes(), sha)
                    # 同じ中身の写しが届いても、自動では走らない。
                    write_script(watch, "orig (2).py", content)
                    window._poll()
                    window._poll()
                    settle_checks(window, ["orig (2).py"])
                    assert pump_until(
                        lambda: False, timeout=0.6) is False
                    assert calls == ["orig.py"], calls
                    dup = item_of(window, "orig (2).py")
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
                    write_script(watch, "other.py",
                                 "print('別の中身です')\n")
                    window._poll()
                    window._poll()
                    settle_checks(window, ["other.py"])
                    assert pump_until(
                        lambda: "other.py" in calls), calls
                    assert pump_until(
                        lambda: window._running_item is None)
                    # 写しの手動実行は妨げない。
                    window._start_run(dup)
                    assert pump_until(
                        lambda: "orig (2).py" in calls), calls
                finally:
                    window.close()
        finally:
            core.execute_script = original_execute
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
    #     last_run は無いので、行のボタンは「実行」で、自動実行
    #     (切替 入) でも走らないこと。
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
                assert row_button_texts(window_b, "warn.py") == ["実行"]
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
                lambda profile: sr._AiAnswerResult(True, "ok"))

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
                    and window._dl_holder is None)
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
                assert bundle.startswith("PHAL診断 Ver1.44 ")
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

        dialog = sr_ai.AiWindowDialog(
            {"process_name": "dummy.exe"}, None,
            filler=fake_filler,
            diag_text_provider=lambda: "束のテキスト")
        try:
            assert dialog._diag_button is not None
            dialog._diag_button.click()
            assert pump_until(lambda: len(filled) == 1)
            assert filled[0][0] == "束のテキスト"
            assert isinstance(filled[0][1], dict)
        finally:
            dialog.close()
        # 作る役が無ければボタンは出ない。
        plain = sr_ai.AiWindowDialog({"process_name": "dummy.exe"}, None)
        try:
            assert plain._diag_button is None
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
                lambda profile: sr._AiAnswerResult(True, "ok"))
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
                    and window._dl_holder is None)
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
                assert fills[0].startswith("PHAL診断 Ver1.44 ")
                # 完了の知らせは、裏方の完了シグナルが届いてから
                # 出る (押下の記録だけ先に立つことがある)。
                assert pump_until(
                    lambda: window.note.text()
                    == "診断を自動で送りました")
                state = core.load_diag_auto()
                assert state["consumed_id"] == "marker-1"
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
                marker3 = {"id": "marker-3"}
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
    assert light["chrome"] is None
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
            # 既定はライトで、今までどおり (地には何も付けない)。
            assert not window.dark_box.isChecked()
            assert window.styleSheet() == ""
            assert window.palette().color(window_role) == \
                window._default_palette.color(window_role)
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
            assert window.styleSheet() == ""
            assert window.palette().color(window_role) == \
                window._default_palette.color(window_role)
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
            dialog = sr_ai.AiWindowDialog(
                {"process_name": "dummy.exe"}, window,
                error_color=window._theme()["dialog_error"])
            try:
                assert dialog._error_color == "#ff7b72"
                dialog.show()
                QCoreApplication.processEvents()
                image = dialog.grab().toImage()
                pixel = image.pixelColor(4, image.height() - 4)
                assert pixel.name() == dark["chrome"]["window"], pixel
            finally:
                dialog.close()
        finally:
            window.close()


if __name__ == "__main__":
    tests = [
        test_window_lists_and_checks,
        test_f5_gating_and_run_copies_result,
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
        test_dark_mode_toggle_and_startup,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
