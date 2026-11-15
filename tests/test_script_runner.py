# tests/test_script_runner.py
# 「スクリプト実行パネル」(script_runner_core.py)の、窓と切り離した純粋部分の
# テスト。チェック(読むだけ)・実行・クリップボード文の組み立て・切り詰め・
# 実行の記録を確かめる。
#
# PowerShell のチェックは powershell.exe のある環境でだけ中身まで確かめ、
# 無い環境(VM)では「チェック不能(unknown)」になることだけ見て飛ばす
# (仕様: PowerShell の無い環境ではテストを飛ばす。PC での確認は別途)。
#
#   python tests/test_script_runner.py
import hashlib
import json
import os
import sys
import tempfile
import threading
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

import script_runner_core as core  # noqa: E402
import script_runner_launch as launch  # noqa: E402

PY = sys.executable
CONFIG = {"python": PY, "timeout_seconds": 120}


def write(tmp: Path, name: str, content) -> Path:
    path = tmp / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")
    return path


# ---------------------------------------------------------------
# 設定
# ---------------------------------------------------------------
def test_normalize_config_defaults_and_clamps():
    config = core.normalize_config(None)
    assert config["recent_hours"] == 24 and config["max_items"] == 20
    assert config["python"] == "python" and config["timeout_seconds"] == 120
    assert config["head_lines"] == 200 and config["tail_lines"] == 50
    assert config["max_chars"] == 20000 and config["always_on_top"] is False
    assert config["dark_mode"] is False
    broken = core.normalize_config({
        "recent_hours": "x", "max_items": 0, "timeout_seconds": 99999,
        "window_geometry": [1, 2], "python": "", "always_on_top": 1,
        "dark_mode": 1})
    assert broken["recent_hours"] == 24
    assert broken["max_items"] == 1
    assert broken["timeout_seconds"] == 3600
    assert broken["window_geometry"] is None
    assert broken["python"] == "python"
    assert broken["always_on_top"] is True
    assert broken["dark_mode"] is True


def test_list_candidates_filters_and_orders():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        now = time.time()
        write(tmp, "a.py", "print(1)\n")
        write(tmp, "b.ps1", "Write-Output 1\n")
        write(tmp, "c.txt", "x")
        write(tmp, "d.py.crdownload", "x")
        write(tmp, "e.py.part", "x")
        old = write(tmp, "old.py", "print(0)\n")
        os.utime(old, (now - 100 * 3600, now - 100 * 3600))
        os.utime(tmp / "a.py", (now - 60, now - 60))
        os.utime(tmp / "b.ps1", (now - 30, now - 30))
        found = core.list_candidates(tmp, now - 24 * 3600, 20)
        names = [p.name for p, _m, _s in found]
        assert names == ["b.ps1", "a.py"], names
        found = core.list_candidates(tmp, now - 24 * 3600, 1)
        assert [p.name for p, _m, _s in found] == ["b.ps1"]


def test_file_sha256_changes_with_content():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "x.py", "print(1)\n")
        first = core.file_sha256(path)
        assert first == hashlib.sha256(b"print(1)\n").hexdigest()
        write(Path(td), "x.py", "print(2)\n")
        assert core.file_sha256(path) != first
        assert core.file_sha256(Path(td) / "none.py") == ""


# ---------------------------------------------------------------
# Python のチェック
# ---------------------------------------------------------------
def test_check_python_ok():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "ok.py",
                     "import os\nimport json\n\nprint(os.name, json.dumps({}))\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result
        assert result.headline == "問題なし"
        assert result.sha256 == core.file_sha256(path)


def test_check_python_unclosed_paren():
    with tempfile.TemporaryDirectory() as td:
        src = ("counts = {}\n"
               "rows = sorted(counts.items(), key=lambda x: x[1]\n"
               "print(rows)\n")
        path = write(Path(td), "bad.py", src)
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_ERROR, result
        assert result.kind == "syntax"
        assert result.issues[0].line == 2
        assert "sorted" in result.issues[0].text
        assert result.jump_line == 2


def test_check_python_indent_error():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "bad.py", "def f():\nreturn 1\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_ERROR, result
        assert result.kind == "syntax"
        assert result.issues[0].line == 2


def test_check_python_missing_import():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "imp.py",
                     "import traytools_no_such_module_zzz\nprint(1)\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_ERROR, result
        assert result.kind == "import"
        assert "traytools_no_such_module_zzz" in result.issues[0].message
        assert result.issues[0].line == 1


def test_check_python_import_from_app_folder():
    # PHAL 本体のフォルダ (script_runner_core.py のあるフォルダ)
    # に実在するモジュールは、スクリプトの置き場に無くても先頭の
    # import が通る (1-53)。実行時の PYTHONPATH と探す場所を
    # そろえたためのもので、今朝止まった phal_lib がその代表。
    # 存在しないモジュールは従来どおり実行不可のまま。
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "uses_lib.py",
                     "import phal_lib\nprint(1)\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result
        path = write(Path(td), "uses_missing.py",
                     "import phal_lib\n"
                     "import traytools_no_such_module_zzz\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_ERROR, result
        assert result.kind == "import"
        assert "traytools_no_such_module_zzz" in result.issues[0].message


def test_execute_script_can_import_app_folder_module():
    # 実行側 (1-53): 本体フォルダのモジュールを、スクリプトの
    # 置き場 (一時フォルダ) に無くても import して実行できる。
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "run_lib.py",
                     "import phal_lib\n"
                     "print('lib-ok', hasattr(phal_lib, 'read_text_auto'))\n")
        run = core.execute_script(path, CONFIG)
        assert run.exit_code == 0, run
        assert "lib-ok True" in run.stdout, run


def test_check_python_import_top_name_only_and_try_excluded():
    with tempfile.TemporaryDirectory() as td:
        # 最上位の名前だけを見る(アオイの追記1)。os の下までは調べない。
        path = write(Path(td), "top.py", "import os.no_such_sub_zzz\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result
        # try の中の import は対象外。
        path = write(Path(td), "tryimp.py",
                     "try:\n"
                     "    import traytools_no_such_module_zzz\n"
                     "except ImportError:\n"
                     "    pass\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result
        # from import も最上位だけ。
        path = write(Path(td), "fromimp.py",
                     "from traytools_no_such_pkg_zzz.sub import thing\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_ERROR, result
        assert result.kind == "import"


def test_check_python_bom_is_warn():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "bom.py", b"\xef\xbb\xbfprint('hi')\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_WARN, result
        assert result.kind == "bom"


def test_check_python_cp932_is_error():
    with tempfile.TemporaryDirectory() as td:
        raw = "# 日本語のコメント\nprint('あ')\n".encode("cp932")
        path = write(Path(td), "sjis.py", raw)
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_ERROR, result
        assert result.kind == "encoding"


def test_check_python_dangerous_calls():
    cases = [
        ("import os\nos.remove('x')\n", "os.remove"),
        ("from pathlib import Path\nPath('x').unlink()\n", "unlink"),
        ("import shutil\nshutil.rmtree('d')\n", "shutil.rmtree"),
        ("import subprocess\nsubprocess.run(['x'])\n", "subprocess"),
        ("eval('1+1')\n", "eval"),
        ("getattr(obj, 'run')()\n", "getattr"),
        ("import ctypes\n", "ctypes"),
        ("open('x.txt', 'w')\n", "open"),
        ("open('x.txt', mode='a')\n", "open"),
        ("open('x.txt', some_mode)\n", "open"),  # モードが読めないときも要確認
        ("from pathlib import Path\nPath('x').write_text('a')\n", "write_text"),
        ("import os\nos.rename('a', 'b')\n", "os.rename"),
        ("import shutil\nshutil.copy('a', 'b')\n", "shutil.copy"),
    ]
    with tempfile.TemporaryDirectory() as td:
        for i, (src, needle) in enumerate(cases):
            path = write(Path(td), f"d{i}.py", src)
            result = core.check_python_script(path, PY)
            assert result.status == core.STATUS_WARN, (src, result)
            assert needle in result.issues[0].message, (src, result)
        # 読むだけの open は問題なし。
        path = write(Path(td), "read.py",
                     "with open('x.txt') as f:\n    print(f.read())\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result
        path = write(Path(td), "read2.py", "open('x.txt', 'r')\n")
        assert core.check_python_script(path, PY).status == core.STATUS_OK


def test_check_python_no_python_exe():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "ok.py", "print(1)\n")
        result = core.check_python_script(
            path, "traytools_no_such_python_zzz")
        assert result.status == core.STATUS_UNKNOWN, result


# ---------------------------------------------------------------
# PowerShell のチェック(powershell.exe が無い環境では飛ばす)
# ---------------------------------------------------------------
def _ps_available() -> bool:
    return bool(core.find_powershell())


def test_check_powershell_unavailable_is_unknown():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "x.ps1", "Write-Output 1\n")
        result = core.check_powershell_script(
            path, powershell_exe="traytools_no_such_ps_zzz")
        assert result.status == core.STATUS_UNKNOWN, result
        assert result.kind == "no_powershell"


def test_check_powershell_cases():
    if not _ps_available():
        # 行頭の "SKIP:" は run_all がファイル全体のスキップと数えるので
        # 使わない(ここは一部だけ飛ばし、他は通っている)。
        print("一部だけ飛ばす: powershell.exe が無いので中身の確認はしない")
        return
    cases = [
        ("Get-ChildItem | Select-Object Name\n", core.STATUS_OK, ""),
        ("Write-Output 'hi'\n", core.STATUS_OK, ""),
        ("Set-Clipboard -Value 'x'\n", core.STATUS_OK, ""),
        ("if ($true) { Write-Output 1\n", core.STATUS_ERROR, "syntax"),
        ("$a = 1 && Write-Output $a\n", core.STATUS_ERROR, "syntax"),
        ("Remove-Item 'x.log'\n", core.STATUS_WARN, "Remove-Item"),
        ("rm 'x.log'\n", core.STATUS_WARN, "Remove-Item"),
        ("iex $cmd\n", core.STATUS_WARN, ""),
        ("Get-ChildItem > out.txt\n", core.STATUS_WARN, "リダイレクト"),
        ("[IO.File]::Delete('x')\n", core.STATUS_WARN, ".NET"),
        ("Start-Process notepad\n", core.STATUS_WARN, "Start-Process"),
    ]
    with tempfile.TemporaryDirectory() as td:
        for i, (src, status, needle) in enumerate(cases):
            path = write(Path(td), f"c{i}.ps1", src)
            result = core.check_powershell_script(path)
            assert result.status == status, (src, result)
            if needle == "syntax":
                assert result.kind == "syntax", (src, result)
            elif needle:
                assert needle in result.headline or any(
                    needle in issue.message for issue in result.issues), \
                    (src, result)


def test_ps_allowlist_matching():
    allowed = core.load_ps_allowlist()
    assert "Get-*" in allowed and "Set-Clipboard" in allowed
    assert core._ps_allowed("Get-ChildItem", allowed)
    assert core._ps_allowed("get-date", allowed)  # 大文字小文字は区別しない
    assert core._ps_allowed("Set-Clipboard", allowed)
    assert not core._ps_allowed("Remove-Item", allowed)
    assert not core._ps_allowed("Invoke-Expression", allowed)


# ---------------------------------------------------------------
# 切り詰めとクリップボード文
# ---------------------------------------------------------------
def test_truncate_lines_and_boundary():
    text = "\n".join(f"line{i}" for i in range(300))
    out = core.truncate_text(text, head_lines=200, tail_lines=50,
                             max_chars=100000)
    lines = out.split("\n")
    assert len(lines) == 251
    assert lines[200] == "(中略 50 行)"
    assert lines[-1] == "line299"
    # ちょうど境目(head+tail 行)は切らない。
    exact = "\n".join(f"line{i}" for i in range(250))
    assert core.truncate_text(exact, 200, 50, 100000) == exact
    # 1行だけ超えたら中略 1 行。
    over = "\n".join(f"line{i}" for i in range(251))
    out = core.truncate_text(over, 200, 50, 100000)
    assert "(中略 1 行)" in out


def test_truncate_chars_and_boundary():
    text = "a" * 20000
    assert core.truncate_text(text, 200, 50, 20000) == text  # ちょうどは切らない
    over = "a" * 20001
    out = core.truncate_text(over, 200, 50, 20000)
    assert len(out) == 20000
    assert "(中略)" in out
    # 行の切り詰めのあとに文字数でも切る。
    text = "\n".join("x" * 100 for _ in range(300))
    out = core.truncate_text(text, 200, 50, 20000)
    assert len(out) <= 20000


def test_build_result_text():
    run = core.RunResult(name="check.py", exit_code=0, seconds=0.84,
                         stdout="hello\n", stderr="")
    text = core.build_result_text("check.py", run, CONFIG)
    lines = text.split("\n")
    assert lines[0] == "[実行結果] check.py  終了コード 0  0.8秒"
    assert "--- 出力 ---\nhello\n" in text
    assert "--- エラー出力 ---\n(なし)" in text
    # 時間切れは1行目に明記。
    run2 = core.RunResult(name="slow.py", exit_code=-9, seconds=120.0,
                          stdout="途中まで\n", timed_out=True)
    text2 = core.build_result_text("slow.py", run2, CONFIG)
    assert text2.split("\n")[0].startswith("[実行結果] slow.py  時間切れ")
    assert "途中まで" in text2
    # 停止も1行目に明記。
    run3 = core.RunResult(name="slow.py", exit_code=-9, seconds=3.0,
                          stopped=True)
    assert "停止しました" in core.build_result_text(
        "slow.py", run3, CONFIG).split("\n")[0]
    # 実行ポリシーで止まったら注意を足す。
    run4 = core.RunResult(name="x.ps1", exit_code=1, seconds=0.5,
                          stderr="PSSecurityException", policy_stop=True)
    assert core.POLICY_NOTE in core.build_result_text("x.ps1", run4, CONFIG)


def test_parse_command_id():
    # 先頭の「セッション番号-通番」+ "_" + 名前、だけを ID とする。
    assert core.parse_command_id("12-03_check_db.py") == "12-03"
    assert core.parse_command_id("12-3_check_db.py") == "12-3"
    assert core.parse_command_id("1-1_a.py") == "1-1"
    assert core.parse_command_id("123-456_x.ps1") == "123-456"
    # ID が無い名前。
    assert core.parse_command_id("check_db.py") is None
    assert core.parse_command_id("x12-3_check.py") is None
    # 日付で始まる名前は ID ではない(4桁の年・"-" 区切りの続き)。
    assert core.parse_command_id("2026-11-14_notes.py") is None
    assert core.parse_command_id("2026-11_notes.py") is None
    # 区切りが "_" でない、桁が多すぎる、名前が続かないものも外す。
    assert core.parse_command_id("12-3-check.py") is None
    assert core.parse_command_id("1234-5_x.py") is None
    assert core.parse_command_id("12-3456_x.py") is None
    assert core.parse_command_id("12-3.py") is None


def test_extract_summary():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 先頭コメントが概要。ID 付きなら「ID: 」の接頭辞は外す。
        demo = write(tmp, "9-1_demo.py", "# 9-1: データを集める\nprint(1)\n")
        assert core.extract_summary(demo) == "データを集める"
        # 「ID 」(コロンなし)の接頭辞も外す。
        bare = write(tmp, "9-2_b.py", "# 9-2 集計する\nprint(1)\n")
        assert core.extract_summary(bare) == "集計する"
        # ID 無しの先頭コメントはそのまま。
        plain = write(tmp, "a.py", "# 掃除する\nprint(1)\n")
        assert core.extract_summary(plain) == "掃除する"
        # 空行と shebang を飛ばした先のコメント。
        sh = write(tmp, "b.py", "\n#!/usr/bin/env python\n# 変換する\n")
        assert core.extract_summary(sh) == "変換する"
        # .phal の宣言行を飛ばした先のコメント。
        ph = write(tmp, "9-3_c.phal", "#!phal python\n# 9-3: 送る\n")
        assert core.extract_summary(ph) == "送る"
        # コードが先なら概要なし。docstring も拾わない。
        code = write(tmp, "c.py", "print(1)\n# 遅いコメント\n")
        assert core.extract_summary(code) == ""
        doc = write(tmp, "d.py", '"""説明"""\nprint(1)\n')
        assert core.extract_summary(doc) == ""
        # 読めないファイルは ""。
        assert core.extract_summary(tmp / "none.py") == ""


def test_build_result_text_with_command_id():
    run = core.RunResult(name="12-3_check.py", exit_code=0, seconds=0.42,
                         stdout="hello\n", stderr="")
    text = core.build_result_text(
        "12-3_check.py", run, CONFIG, command_id="12-3")
    assert text.split("\n")[0] == \
        "[実行結果] 12-3 12-3_check.py  終了コード 0  0.4秒"
    # ID を渡さなければ今までと完全に同じ文になる。
    plain = core.build_result_text("12-3_check.py", run, CONFIG)
    assert plain.split("\n")[0] == \
        "[実行結果] 12-3_check.py  終了コード 0  0.4秒"
    # 時間切れ・停止の1行目にも ID が入る。
    run2 = core.RunResult(name="12-3_slow.py", exit_code=-9, seconds=120.0,
                          timed_out=True)
    text2 = core.build_result_text(
        "12-3_slow.py", run2, CONFIG, command_id="12-3")
    assert text2.split("\n")[0].startswith("[実行結果] 12-3 12-3_slow.py")
    run3 = core.RunResult(name="12-3_slow.py", exit_code=-9, seconds=3.0,
                          stopped=True)
    text3 = core.build_result_text(
        "12-3_slow.py", run3, CONFIG, command_id="12-3")
    assert text3.split("\n")[0] == "[実行結果] 12-3 12-3_slow.py  停止しました"


# ---------------------------------------------------------------
# 長い出力の要約モード
# ---------------------------------------------------------------
class _temp_data_dir:
    """TRAYTOOLS_DATA_DIR を一時フォルダへ向ける(使い終わったら戻す)。

    要約モードは全文を data_dir() の下へ保存するので、テストでは
    本物の置き場へ書かないよう差し替える。"""

    def __init__(self, path):
        self.path = str(path)
        self.old = os.environ.get("TRAYTOOLS_DATA_DIR")

    def __enter__(self):
        os.environ["TRAYTOOLS_DATA_DIR"] = self.path
        return Path(self.path)

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = self.old
        return False


def test_summary_short_output_is_not_summarized():
    # 短い出力は要約にならず、従来の文のまま (末尾に AI への
    # 指示文が付くのが 1.55 での唯一の追加)。
    run = core.RunResult(name="check.py", exit_code=0, seconds=0.84,
                         stdout="hello\n", stderr="")
    text = core.build_result_text("check.py", run, CONFIG)
    assert text == ("[実行結果] check.py  終了コード 0  0.8秒\n"
                    "--- 出力 ---\nhello\n"
                    "--- エラー出力 ---\n(なし)\n"
                    + core.RESULT_AI_PROMPT)
    # ちょうど summary_lines 行(既定 100 行)では要約にならない。
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td)):
            out = "\n".join(f"line{i}" for i in range(100)) + "\n"
            run2 = core.RunResult(name="edge.py", exit_code=0, seconds=1.0,
                                  stdout=out, stderr="")
            text2 = core.build_result_text("edge.py", run2, CONFIG)
            assert "要約" not in text2 and "全文:" not in text2
            assert "line99" in text2
            # 全文ファイルも作られない。
            assert not (Path(td) / "phal-outputs").exists()


def test_summary_long_output():
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td)) as data:
            out = "\n".join(f"out{i}" for i in range(150)) + "\n"
            err = "\n".join(f"err{i}" for i in range(12)) + "\n"
            run = core.RunResult(name="long.py", exit_code=0, seconds=1.0,
                                 stdout=out, stderr=err)
            text = core.build_result_text("long.py", run, CONFIG)
            lines = text.split("\n")
            assert lines[0] == \
                "[実行結果] long.py  終了コード 0  1.0秒・全162行"
            assert "--- 出力（要約・全150行） ---" in text
            # 先頭15行と末尾10行が入り、中間は入らない。
            for i in list(range(15)) + list(range(140, 150)):
                assert f"out{i}" in text, i
            assert "out75" not in text
            assert "…（中略：125行）…" in text
            # エラー出力は先頭10行だけ。残りは件数で知らせる。
            assert "--- エラー出力（先頭・全12行） ---" in text
            for i in range(10):
                assert f"err{i}" in text, i
            assert "err10" not in text and "err11" not in text
            assert "…あと2行" in text
            # 全文の置き場と、取り出し方の案内がある。
            full_line = [l for l in lines if l.startswith("全文: ")]
            assert len(full_line) == 1
            full_path = Path(full_line[0][len("全文: "):])
            assert full_path.is_file()
            assert full_path.parent == data / "phal-outputs"
            assert "out75" in full_path.read_text(encoding="utf-8")
            assert "「○行目付近を見せて」と頼めば取り出せます" in text
            # 実行ポリシーで止まったら注意も末尾に付く。
            run2 = core.RunResult(name="x.ps1", exit_code=1, seconds=0.5,
                                  stdout=out, stderr="PSSecurityException",
                                  policy_stop=True)
            text2 = core.build_result_text("x.ps1", run2, CONFIG)
            assert core.POLICY_NOTE in text2
            # 注意は指示文の直前 (末尾は AI への指示文で固定、1.55)。
            assert text2.split("\n")[-2] == core.POLICY_NOTE
            assert text2.split("\n")[-1] == core.RESULT_AI_PROMPT


def test_summary_chars_trigger():
    # 行数は少なくても文字数が summary_chars を超えたら要約になる。
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td)):
            run = core.RunResult(name="wide.py", exit_code=0, seconds=0.5,
                                 stdout="x" * 9000 + "\n", stderr="")
            text = core.build_result_text("wide.py", run, CONFIG)
            assert "・全1行" in text.split("\n")[0]
            assert "--- 出力（要約・全1行） ---" in text
            assert "全文: " in text


def test_summary_repeated_lines_collapsed():
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td)):
            out = "\n".join(["pong"] * 110 + ["done"] * 5) + "\n"
            run = core.RunResult(name="rep.py", exit_code=0, seconds=1.0,
                                 stdout=out, stderr="")
            text = core.build_result_text("rep.py", run, CONFIG)
            assert "pong（同じ行が 15 回）" in text
            assert "pong（同じ行が 5 回）" in text
            assert "done（同じ行が 5 回）" in text
            assert "pong\npong" not in text
            # 空行はまとめない。
            collapsed = core._collapse_repeated_lines(
                ["a", "", "", "a", "a", " ", "b"])
            assert collapsed == ["a", "", "", "a（同じ行が 2 回）",
                                 " ", "b"]


def test_summary_mode_off_keeps_truncation():
    # summary_mode が偽なら、要約にはならず従来の切り詰め文になる。
    off = dict(CONFIG, summary_mode=False)
    out = "\n".join(f"out{i}" for i in range(150)) + "\n"
    run = core.RunResult(name="long.py", exit_code=0, seconds=1.0,
                         stdout=out, stderr="")
    text = core.build_result_text("long.py", run, off)
    assert "要約" not in text and "全文:" not in text
    assert "out149" in text
    # 文字数の切り詰めも従来どおり。
    run2 = core.RunResult(name="wide.py", exit_code=0, seconds=0.5,
                          stdout="y" * 25000 + "\n", stderr="")
    text2 = core.build_result_text("wide.py", run2, off)
    assert "(中略)" in text2 and "要約" not in text2


def test_save_full_output():
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td)) as data:
            run = core.RunResult(name="1-2_a.py", exit_code=0, seconds=1.23,
                                 stdout="a\nb\n", stderr="e\n")
            path = core.save_full_output(run, CONFIG, command_id="1-2")
            assert path is not None
            saved = Path(path)
            assert saved.parent == data / "phal-outputs"
            assert saved.name.startswith("1-2_")
            assert saved.name.endswith(".txt")
            raw = saved.read_bytes()
            assert b"\r" not in raw  # LF のまま
            body = raw.decode("utf-8")
            assert body == ("[実行結果] 1-2 1-2_a.py  終了コード 0  "
                            "1.2秒・全3行\n"
                            "--- 出力 ---\na\nb\n"
                            "\n--- エラー出力 ---\ne\n")
            # ID が無ければ noid になる。
            run2 = core.RunResult(name="b.py", exit_code=0, seconds=0.1,
                                  stdout="x\n", stderr="")
            path2 = core.save_full_output(run2, CONFIG)
            assert Path(path2).name.startswith("noid_")


def test_save_full_output_failure_falls_back():
    # 保存に失敗したら None になり、build_result_text は
    # 従来の切り詰め文に落とす(要約を諦めても結果は返す)。
    with tempfile.TemporaryDirectory() as td:
        blocker = Path(td) / "blocker"
        blocker.write_text("x", encoding="utf-8", newline="")
        with _temp_data_dir(blocker):
            run = core.RunResult(name="long.py", exit_code=0, seconds=1.0,
                                 stdout="z\n" * 150, stderr="")
            assert core.save_full_output(run, CONFIG) is None
            text = core.build_result_text("long.py", run, CONFIG)
            assert "要約" not in text and "全文:" not in text
            assert "--- 出力 ---\n" in text


def test_normalize_config_summary_defaults_and_clamps():
    config = core.normalize_config(None)
    assert config["summary_mode"] is True
    assert config["summary_lines"] == 100
    assert config["summary_chars"] == 8000
    broken = core.normalize_config({
        "summary_mode": 0, "summary_lines": 5, "summary_chars": 50})
    assert broken["summary_mode"] is False
    assert broken["summary_lines"] == 10
    assert broken["summary_chars"] == 100
    big = core.normalize_config({
        "summary_lines": 999999, "summary_chars": 99999999})
    assert big["summary_lines"] == 100000
    assert big["summary_chars"] == 1000000
    bad = core.normalize_config({
        "summary_lines": "x", "summary_chars": "x"})
    assert bad["summary_lines"] == 100
    assert bad["summary_chars"] == 8000


def test_build_return_text():
    run = core.RunResult(name="5-2_fix.py", exit_code=3, seconds=0.7,
                         stdout="途中まで\n", stderr="Traceback: boom\n")
    body = "import sys\nsys.exit(3)\n"
    text = core.build_return_text(
        "5-2_fix.py", "直っていません", run, CONFIG, body,
        command_id="5-2")
    lines = text.split("\n")
    assert lines[0] == "[差し戻し] 5-2 5-2_fix.py", lines[0]
    assert lines[1] == "理由: 直っていません"
    assert "--- 実行結果 ---" in lines
    assert "[実行結果] 5-2 5-2_fix.py  終了コード 3  0.7秒" in text
    assert "--- エラー出力 ---\nTraceback: boom" in text
    assert "--- スクリプト ---\nimport sys\nsys.exit(3)" in text
    # 理由が空なら行ごと省く。実行結果が無ければその段も無い。
    plain = core.build_return_text("plain.py", "", None, CONFIG, body)
    assert plain.split("\n")[0] == "[差し戻し] plain.py"
    assert "理由:" not in plain and "--- 実行結果 ---" not in plain
    assert "--- スクリプト ---\nimport sys\nsys.exit(3)" in plain
    # ゴミ箱は Windows 専用。ここ(VM)では常に False で、例外にもならない。
    assert core.move_to_recycle_bin(Path("nope.py")) is False


def test_build_check_text():
    result = core.CheckResult(
        status=core.STATUS_ERROR, kind="syntax",
        issues=[core.CheckIssue(
            line=14, message="括弧が閉じていません",
            text="rows = sorted(counts.items(), key=lambda x: x[1]",
            offset=18)])
    text = core.build_check_text("check_retx.py", result)
    lines = text.split("\n")
    assert lines[0] == "[実行前チェック] check_retx.py  文法エラー 1件"
    assert lines[1] == "14行目: 括弧が閉じていません"
    assert lines[2] == "    rows = sorted(counts.items(), key=lambda x: x[1]"
    assert lines[3].strip() == "^"
    assert lines[3].index("^") == 4 + 18 - 1
    assert lines[4] == "このエラーを直したスクリプトを、もう一度ください。"


def test_detect_policy_stop():
    assert core.detect_policy_stop(
        "PSSecurityException: running scripts is disabled on this system")
    assert core.detect_policy_stop("実行ポリシーについて詳しくは…")
    assert not core.detect_policy_stop("普通のエラー")


# ---------------------------------------------------------------
# ループ記録の Markdown まとめ (1.51)
# ---------------------------------------------------------------
def _loop_record(command_id="1-81", name="1-81_check_db.py",
                 summary="DB の件数を数える",
                 at="2026-11-15 08:20:11", stdout="count: 42\n",
                 stderr="", exit_code=0, seconds=12.3,
                 full_output_path=""):
    run = core.RunResult(name=name, exit_code=exit_code, seconds=seconds,
                         stdout=stdout, stderr=stderr)
    return core.LoopRecord(
        at=at, command_id=command_id, name=name, summary=summary, run=run,
        full_output_path=full_output_path)


def test_build_loop_record_markdown_empty():
    assert core.build_loop_record_markdown([]) == ""
    assert core.build_loop_record_markdown(None) == ""
    assert core.build_loop_record_markdown([None]) == ""


def test_build_loop_record_markdown_two_hands():
    first = _loop_record()
    second = _loop_record(
        command_id="1-82", name="1-82_fix.py", summary="件数を直す",
        at="2026-11-15 08:21:05", stdout="```python\nprint(1)\n```\n",
        stderr="warn: x\n", exit_code=0, seconds=0.5)
    text = core.build_loop_record_markdown([first, second])
    assert text.startswith("# PHAL ループ記録（直近2手）\n"), text
    # 古い順に並ぶ。見出しはコマンドIDと時刻。
    assert text.index("## 1-81 2026-11-15 08:20:11") \
        < text.index("## 1-82 2026-11-15 08:21:05")
    assert "- ねらい：DB の件数を数える" in text
    assert "- スクリプト：1-81_check_db.py" in text
    assert "- 結果：終了コード 0・12.3秒" in text
    assert "- ねらい：件数を直す" in text
    assert "- 結果：終了コード 0・0.5秒" in text
    assert "count: 42" in text
    assert "- エラー出力（全1行）：" in text and "warn: x" in text
    # 出力に ``` が混ざっても、囲みは1つ長くなるので壊れない。
    assert "````\n```python\nprint(1)\n```\n````" in text, text
    # 21手あれば直近20手だけ残り、いちばん古い手は落ちる。
    many = [_loop_record(command_id=f"1-{i:02d}",
                         name=f"1-{i:02d}_x.py",
                         at=f"2026-11-15 08:{i:02d}:00")
            for i in range(1, 22)]
    kept = core.build_loop_record_markdown(many)
    assert kept.startswith("# PHAL ループ記録（直近20手）\n"), kept
    assert "## 1-01 " not in kept and "## 1-02 " in kept


def test_build_loop_record_markdown_long_output():
    stdout = "".join(f"line {i}\n" for i in range(150))
    stderr = "".join(f"err {i}\n" for i in range(15))
    record = _loop_record(
        stdout=stdout, stderr=stderr,
        full_output_path="C:/data/phal-outputs/1-81_x_ab12cd34.txt")
    text = core.build_loop_record_markdown([record])
    assert "- 出力（全150行）：" in text
    assert "line 0\n" in text and "line 149\n" in text
    assert "…（中略：125行）…" in text
    assert "line 75\n" not in text
    assert "- 全文：C:/data/phal-outputs/1-81_x_ab12cd34.txt" in text
    # エラー出力は先頭10行だけ。
    assert "err 0\n" in text and "err 14\n" not in text
    assert "…あと5行" in text


def test_build_loop_record_markdown_no_command_id_and_timeout():
    run = core.RunResult(name="plain.py", exit_code=None, seconds=120.0,
                         stdout="", stderr="", timed_out=True)
    record = core.LoopRecord(
        at="2026-11-15 08:22:00", command_id="", name="plain.py",
        summary="", run=run)
    text = core.build_loop_record_markdown([record])
    # ID が無ければ見出しはファイル名。ねらいが無ければ（なし）。
    assert "## plain.py 2026-11-15 08:22:00" in text, text
    assert "- ねらい：（なし）" in text
    assert "- 結果：時間切れで止めました（120秒）" in text
    assert "- 出力：（なし）" in text
    # 停止と起動エラーの行も同じ形で出す。
    stopped = _loop_record(stdout="", seconds=1.0)
    stopped.run.stopped = True
    failed = _loop_record(stdout="", seconds=0.0)
    failed.run.start_error = "起動できませんでした"
    text2 = core.build_loop_record_markdown([stopped, failed])
    assert "- 結果：停止しました" in text2
    assert "起動エラー: 起動できませんでした" in text2


def test_save_full_output_if_long():
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td)) as data:
            short = core.RunResult(name="a.py", exit_code=0, seconds=0.1,
                                   stdout="ok\n", stderr="")
            assert core.save_full_output_if_long(short, CONFIG) is None
            assert not (data / "phal-outputs").exists()
            long_run = core.RunResult(
                name="b.py", exit_code=0, seconds=1.0,
                stdout="z\n" * 150, stderr="")
            path = core.save_full_output_if_long(
                long_run, CONFIG, command_id="1-83")
            assert path is not None
            assert Path(path).parent == data / "phal-outputs"
            # summary_mode が切れていれば、長くても保存しない。
            off = dict(CONFIG, summary_mode=False)
            assert core.save_full_output_if_long(long_run, off) is None


def test_build_result_text_reuses_full_output_path():
    # 長い出力の全文を先に保存しておけば、build_result_text は
    # そのパスを使い、二重には保存しない。
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td)) as data:
            run = core.RunResult(name="1-83_long.py", exit_code=0,
                                 seconds=2.0, stdout="z\n" * 150,
                                 stderr="")
            path = core.save_full_output_if_long(
                run, CONFIG, command_id="1-83")
            assert path is not None
            text = core.build_result_text(
                "1-83_long.py", run, CONFIG, command_id="1-83",
                full_output_path=path)
            assert f"全文: {path}" in text
            saved = list((data / "phal-outputs").glob("*.txt"))
            assert len(saved) == 1, saved
            # 渡さなければ従来どおり自分で保存する (同じ秒・同じ
            # 中身なら同じファイル名で上書きされるので、件数は
            # 数えずパスの中身で見る)。
            text2 = core.build_result_text(
                "1-83_long.py", run, CONFIG, command_id="1-83")
            assert "全文: " in text2
            path2 = text2.split("全文: ", 1)[1].split("\n", 1)[0].strip()
            assert Path(path2).parent == data / "phal-outputs"
            assert Path(path2).exists()


def test_result_ai_prompt_appended():
    # 実行結果の末尾には AI への指示文が必ず付く (1.55)。
    prompt = core.RESULT_AI_PROMPT
    assert prompt.startswith("AIへの指示：")
    # 純粋関数: 末尾に付ける。末尾の改行は落として1行に寄せる。
    assert core.with_result_ai_prompt("本文") == "本文\n" + prompt
    assert core.with_result_ai_prompt("本文\n") == "本文\n" + prompt
    # 既に付いていたら二重に付けない。
    once = core.with_result_ai_prompt("本文")
    assert core.with_result_ai_prompt(once) == once
    # 通常の結果文の末尾に付く (1つだけ)。
    run = core.RunResult(name="check.py", exit_code=0, seconds=0.5,
                         stdout="hello\n", stderr="")
    text = core.build_result_text("check.py", run, CONFIG)
    assert text.endswith(prompt), text
    assert text.count(prompt) == 1
    # 要約モードの結果文にも付く。
    long_run = core.RunResult(name="long.py", exit_code=0, seconds=1.0,
                              stdout="z\n" * 150, stderr="")
    text2 = core.build_result_text(
        "long.py", long_run, CONFIG, full_output_path="dummy")
    assert "全文: dummy" in text2
    assert text2.endswith(prompt), text2
    assert text2.count(prompt) == 1
    # ループ記録 (Markdown まとめ) には付けない。
    record = core.LoopRecord(at="2026-11-15 09:30:00",
                             name="check.py", run=run)
    md = core.build_loop_record_markdown([record], CONFIG)
    assert prompt not in md


def test_diff_output_lines():
    # 行同士の差分: 増えた行・消えた行を、出てきた順・重複なしで
    # 返す (1.56)。
    added, removed = core.diff_output_lines(
        ["a", "b", "c"], ["b", "c", "d", "e"])
    assert added == ["d", "e"], added
    assert removed == ["a"], removed
    # 差分なし。並び順と、同じ行の回数の違いは差分にしない。
    added, removed = core.diff_output_lines(["a", "b", "a"], ["b", "a"])
    assert added == [] and removed == []
    # 重複した新しい行は1回だけ数える。
    added, removed = core.diff_output_lines([], ["x", "x", "y"])
    assert added == ["x", "y"], added
    assert removed == []
    # 上限はここでは付けない (表示の打ち切りは build_result_diff)。
    many_prev = [f"old{i}" for i in range(15)]
    many_cur = [f"new{i}" for i in range(15)]
    added, removed = core.diff_output_lines(many_prev, many_cur)
    assert len(added) == 15 and added[0] == "new0", added
    assert len(removed) == 15 and removed[0] == "old0", removed


def test_build_result_diff_added_only():
    # 差分: 増えた行だけ。見出しは「差分：追加 N行・削除 M行
    # (前回比)」で、増えた行だけが + 付きで出る (1.56)。
    diff = core.build_result_diff(
        "--- 出力 ---\n同じ行\n", "--- 出力 ---\n同じ行\n増える行\n")
    assert diff.split("\n")[0] == "差分：追加 1行・削除 0行（前回比）", diff
    assert "+ 増える行" in diff, diff
    assert "同じ行" not in diff, diff


def test_build_result_diff_removed_only():
    # 差分: 消えた行だけ。
    diff = core.build_result_diff(
        "--- 出力 ---\n同じ行\n消える行\n", "--- 出力 ---\n同じ行\n")
    assert diff.split("\n")[0] == "差分：追加 0行・削除 1行（前回比）", diff
    assert "- 消える行" in diff, diff
    assert "+ " not in diff, diff


def test_build_result_diff_identical():
    # 前回と同じなら「差分なし（前回と同じ結果）」の1行だけ。
    assert core.build_result_diff("a\nb\n", "a\nb\n") \
        == "差分なし（前回と同じ結果）"


def test_build_result_diff_no_previous():
    # 前回の結果が無ければ、今回の全文に「前回結果なし」の注記。
    diff = core.build_result_diff(None, "--- 出力 ---\nhello\n")
    assert diff == "--- 出力 ---\nhello\n（前回結果なし）", diff
    # 今回が空でも注記だけは出る。
    assert core.build_result_diff(None, "") == "（前回結果なし）"


def test_build_result_diff_trailing_newline():
    # 末尾の改行の有無と CRLF は差分にしない。
    assert core.build_result_diff("a\nb\n", "a\nb") \
        == "差分なし（前回と同じ結果）"
    assert core.build_result_diff("a\r\nb\r\n", "a\nb\n") \
        == "差分なし（前回と同じ結果）"
    # 末尾以外の改行の違いは差分になる。
    diff = core.build_result_diff("a\nb\n", "a\n\nb\n")
    assert diff.startswith("差分：追加 1行・削除 0行"), diff


def test_build_result_diff_caps_lines_and_notes_truncation():
    # 増えた行が上限 (10行) を超えたら、見出しの件数は全部で、
    # 出すのは先頭10行までで「ほか N行」と書く。
    prev = "\n".join(f"old{i}" for i in range(3))
    new = "\n".join([f"new{i}" for i in range(12)] + ["old0"])
    diff = core.build_result_diff(prev, new)
    assert diff.split("\n")[0] == "差分：追加 12行・削除 2行（前回比）", diff
    assert "+ new9" in diff and "+ new10" not in diff, diff
    assert "（ほか 2行）" in diff, diff
    assert "- old1" in diff and "- old2" in diff, diff
    # 前回が2万字で打ち切られていたら、その旨を末尾に足す。
    diff2 = core.build_result_diff(prev, new, previous_truncated=True)
    assert diff2.endswith("（前回の結果は2万字で打ち切られています）"), diff2


def test_build_rerun_result_text():
    # 再実行の結果文: 見出し行は通常と同じで、その下は差分だけ。
    # 全文の「--- 出力 ---」の節は出ない。末尾は AI への指示文。
    run = core.RunResult(name="check.py", exit_code=0, seconds=0.6,
                         stdout="同じ行\n増える行\n",
                         stderr="新しい警告\n")
    prev_body = core.build_result_body(
        "check.py",
        core.RunResult(name="check.py", exit_code=0, seconds=0.5,
                       stdout="同じ行\n消える行\n", stderr=""),
        CONFIG)
    prev_text = core.result_diff_source(prev_body)
    text = core.build_rerun_result_text(
        "check.py", run, CONFIG, previous_text=prev_text)
    lines = text.split("\n")
    assert lines[0].startswith("[実行結果] check.py"), text
    assert "差分：追加 2行・削除 2行（前回比）" in text, text
    assert "+ 増える行" in text and "+ 新しい警告" in text, text
    assert "- 消える行" in text and "- (なし)" in text, text
    assert "--- 出力 ---" not in text, text
    assert "同じ行" not in text, text
    assert text.endswith(core.RESULT_AI_PROMPT), text
    assert text.count(core.RESULT_AI_PROMPT) == 1
    # 前回と同じなら「差分なし」の1行だけ (見出し＋1行＋指示文)。
    same = core.RunResult(name="check.py", exit_code=0, seconds=0.7,
                          stdout="同じ行\n消える行\n", stderr="")
    text2 = core.build_rerun_result_text(
        "check.py", same, CONFIG, previous_text=prev_text)
    lines2 = text2.split("\n")
    assert len(lines2) == 3, text2
    assert lines2[0].startswith("[実行結果] check.py"), text2
    assert lines2[1] == "差分なし（前回と同じ結果）", text2
    assert lines2[2] == core.RESULT_AI_PROMPT, text2
    # 前回が無ければ全文＋「前回結果なし」の注記。
    text3 = core.build_rerun_result_text("check.py", run, CONFIG)
    assert "--- 出力 ---\n同じ行\n増える行" in text3, text3
    assert "（前回結果なし）" in text3, text3
    assert text3.endswith(core.RESULT_AI_PROMPT), text3
    # 要約モードの長い出力は、要約文が差分の対象になる (全文
    # 比較はしない)。
    long_prev = core.RunResult(name="long.py", exit_code=0, seconds=1.0,
                               stdout="z\n" * 150, stderr="")
    long_run = core.RunResult(name="long.py", exit_code=0, seconds=1.0,
                              stdout="z\n" * 149 + "新しい行\n",
                              stderr="")
    prev_long = core.result_diff_source(core.build_result_body(
        "long.py", long_prev, CONFIG, full_output_path="dummy"))
    text4 = core.build_rerun_result_text(
        "long.py", long_run, CONFIG,
        previous_text=prev_long, full_output_path="dummy")
    # 要約文の畳んだ形が差分の対象 (全文 150 行そのままではない)。
    assert "+ 新しい行" in text4, text4
    assert "+ z（同じ行が 9 回）" in text4, text4
    assert "- z（同じ行が 10 回）" in text4, text4
    # 「全文: dummy」の行は両方に共通なので、差分だけの文には出ない。
    assert "全文: dummy" not in text4, text4
    assert text4.endswith(core.RESULT_AI_PROMPT), text4


def test_last_results_roundtrip_limit_and_truncation():
    # 前回の結果の控え: 書き出しと読み込み、20件の上限、2万字の
    # 打ち切り、壊れたファイルは空に倒す (1.56)。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = tmp / "phal_last_results.json"
        records = core.load_last_results(path)
        assert records == {}
        core.last_results_put(records, "1-1", "結果1")
        core.last_results_put(records, "1-2", "結果2")
        core.save_last_results(records, path)
        loaded = core.load_last_results(path)
        assert loaded == {"1-1": {"text": "結果1", "truncated": False},
                          "1-2": {"text": "結果2", "truncated": False}}, loaded
        # 2万字を超えたら打ち切り、truncated が立つ。
        core.last_results_put(records, "1-3", "x" * 20001)
        assert len(records["1-3"]["text"]) == 20000
        assert records["1-3"]["truncated"] is True
        core.last_results_put(records, "1-4", "y" * 20000)
        assert records["1-4"]["truncated"] is False
        # 21件目でいちばん古いものから落ちる。
        for n in range(5, 23):
            core.last_results_put(records, f"1-{n}", f"結果{n}")
        assert len(records) == 20, len(records)
        assert "1-1" not in records and "1-2" not in records
        assert "1-22" in records
        # 同じ id の入れ直しは新しい方で置き換わり、古い扱いに
        # ならない。
        core.last_results_put(records, "1-3", "入れ直し")
        assert records["1-3"]["text"] == "入れ直し"
        # 壊れたファイルは空に倒す。形の違う項目は落とす。
        path.write_text("{壊れた", encoding="utf-8")
        assert core.load_last_results(path) == {}
        path.write_text(json.dumps(
            {"ok": {"text": "t", "truncated": False},
             "bad": "文字列", "bad2": {"text": 123}},
            ensure_ascii=False), encoding="utf-8")
        assert core.load_last_results(path) == {
            "ok": {"text": "t", "truncated": False}}



def test_dl_session_roundtrip_and_continuation():
    # 開き直し引き継ぎ: 書き出しと読み込み、続き判定は閉じてから
    # 180秒以内で key があるときだけ (1.70)。
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "script_runner_dl_session.json"
        assert core.load_dl_session(path) == {}
        assert core.dl_session_continuation_key({}) is None
        core.save_dl_session("候補key", path)
        session = core.load_dl_session(path)
        assert session["candidate_key"] == "候補key", session
        closed_at = session["closed_at"]
        assert core.dl_session_continuation_key(
            session, now=closed_at + 60) == "候補key"
        assert core.dl_session_continuation_key(
            session, now=closed_at + 181) is None
        # key が空なら続き扱いにしない。
        core.save_dl_session(None, path)
        session = core.load_dl_session(path)
        assert core.dl_session_continuation_key(
            session, now=session["closed_at"] + 1) is None
        # 壊れたファイルは空に倒す。
        path.write_text("{壊れた", encoding="utf-8")
        assert core.load_dl_session(path) == {}


# ---------------------------------------------------------------
# 実行
# ---------------------------------------------------------------
def test_execute_ok_and_nonzero_and_stderr_only():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = write(tmp, "ok.py", "print('hello')\n")
        run = core.execute_script(path, CONFIG)
        assert run.exit_code == 0 and "hello" in run.stdout
        assert run.sha256 == core.file_sha256(path)
        path = write(tmp, "nz.py", "import sys\nsys.exit(3)\n")
        run = core.execute_script(path, CONFIG)
        assert run.exit_code == 3
        path = write(tmp, "err.py",
                     "import sys\nsys.stderr.write('only error\\n')\n")
        run = core.execute_script(path, CONFIG)
        assert run.exit_code == 0 and run.stdout == ""
        assert "only error" in run.stderr


def test_execute_japanese_output():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "jp.py", "print('こんにちは世界')\n")
        run = core.execute_script(path, CONFIG)
        assert run.exit_code == 0
        assert "こんにちは世界" in run.stdout


def test_phal_engine_check_and_execute():
    # .phal は1行目の宣言で言語を決める。宣言の解釈(BOM・大文字
    # 小文字・空白)、Python 宣言のチェックと実行、宣言なしの
    # エラー、一覧への出現までをまとめて見る。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = write(tmp, "9-1_hi.phal", "#!phal python\nprint('phal-ok')\n")
        assert core.phal_engine(path) == "python"
        ps = write(tmp, "9-2_a.phal", "#!phal powershell\nWrite-Output 1\n")
        assert core.phal_engine(ps) == "powershell"
        bom = tmp / "9-3_b.phal"
        bom.write_bytes(b"\xef\xbb\xbf#!PHAL Python \nprint(1)\n")
        assert core.phal_engine(bom) == "python"
        # 一覧の対象にもなる。
        names = [p.name for p, _m, _s in core.list_candidates(tmp, 0, 10)]
        assert "9-1_hi.phal" in names
        # Python 宣言: チェックが通って、そのまま実行できる。
        assert core.check_script(path, CONFIG).status == core.STATUS_OK
        run = core.execute_script(path, CONFIG)
        assert run.exit_code == 0 and "phal-ok" in run.stdout
        # 宣言なし: 専用の見出しでエラーになる。
        bad = write(tmp, "9-4_bad.phal", "print('x')\n")
        assert core.phal_engine(bad) is None
        result = core.check_script(bad, CONFIG)
        assert result.status == core.STATUS_ERROR
        assert result.headline == \
            "1行目に #!phal python または #!phal powershell が必要です"


def test_execute_timeout_kills_tree():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "slow.py",
                     "import time\nprint('start', flush=True)\ntime.sleep(60)\n")
        config = dict(CONFIG, timeout_seconds=1)
        started = time.monotonic()
        run = core.execute_script(path, config)
        elapsed = time.monotonic() - started
        assert run.timed_out
        assert elapsed < 20, elapsed
        assert "start" in run.stdout  # そこまでの出力は取れる


def test_execute_stop_button():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "slow.py", "import time\ntime.sleep(60)\n")
        handle = core.RunHandle()
        timer = threading.Timer(0.5, handle.stop)
        timer.start()
        run = core.execute_script(path, CONFIG, handle)
        timer.cancel()
        assert run.stopped
        assert run.seconds < 20


def test_execute_missing_python():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "ok.py", "print(1)\n")
        run = core.execute_script(
            path, {"python": "traytools_no_such_python_zzz",
                   "timeout_seconds": 10})
        assert run.exit_code is None
        assert run.start_error


def test_append_run_log():
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "sub" / "log.jsonl"
        run = core.RunResult(name="a.py", path="C:/x/a.py", exit_code=0,
                             seconds=1.25, stdout="出力です", sha256="ab12")
        check = core.CheckResult(status=core.STATUS_OK, kind="")
        core.append_run_log(run, check, log_path)
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["file"] == "a.py" and record["sha256"] == "ab12"
        assert record["exit_code"] == 0 and record["check"] == "ok"
        assert record["stdout_head"] == "出力です"
        assert record["time"]


def test_check_script_dispatch():
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "ok.py", "print(1)\n")
        assert core.check_script(path, CONFIG).status == core.STATUS_OK
        ps = write(Path(td), "x.ps1", "Write-Output 1\n")
        result = core.check_script(ps, CONFIG)
        if _ps_available():
            assert result.status == core.STATUS_OK, result
        else:
            assert result.status == core.STATUS_UNKNOWN, result


# ---------------------------------------------------------------
# レビュー req-20261114-13 の追加分
# ---------------------------------------------------------------
def test_list_candidates_excludes_bom_copies_and_stable_order():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        now = time.time()
        write(tmp, core.BOM_COPY_PREFIX + "x.ps1", "Write-Output 1\n")
        write(tmp, "b.py", "print(1)\n")
        write(tmp, "a.py", "print(1)\n")
        for name in ("b.py", "a.py", core.BOM_COPY_PREFIX + "x.ps1"):
            os.utime(tmp / name, (now - 60, now - 60))
        found = core.list_candidates(tmp, now - 3600, 20)
        names = [p.name for p, _m, _s in found]
        # 写しは出ない。同点は名前順で安定する。
        assert names == ["a.py", "b.py"], names


def test_check_python_danger_respects_import_aliases():
    cases = [
        ("from os import remove\nremove('x')\n", "os.remove"),
        ("from shutil import rmtree\nrmtree('d')\n", "shutil.rmtree"),
        ("import os as o\no.remove('x')\n", "os.remove"),
        ("import os\nos.popen('x')\n", "os.popen"),
        ("import os\nos.startfile('x')\n", "os.startfile"),
        ("import os\nos.makedirs('d')\n", "os.makedirs"),
        ("import shutil\nshutil.copyfile('a', 'b')\n", "shutil.copyfile"),
        ("from pathlib import Path\nPath('x').open('w')\n", "open"),
        ("from pathlib import Path\nPath('x').touch()\n", "touch"),
        ("from pathlib import Path\nPath('x').mkdir()\n", "mkdir"),
    ]
    with tempfile.TemporaryDirectory() as td:
        for i, (src, needle) in enumerate(cases):
            path = write(Path(td), f"a{i}.py", src)
            result = core.check_python_script(path, PY)
            assert result.status == core.STATUS_WARN, (src, result)
            assert any(needle in issue.message
                       for issue in result.issues), (src, result)
        negatives = [
            "from os import getcwd\nprint(getcwd())\n",
            "from pathlib import Path\nPath('x').open()\n",
            "from pathlib import Path\nPath('x').open('r')\n",
            "from pathlib import Path\nprint(Path('x').read_text())\n",
        ]
        for i, src in enumerate(negatives):
            path = write(Path(td), f"n{i}.py", src)
            result = core.check_python_script(path, PY)
            assert result.status == core.STATUS_OK, (src, result)


def test_check_python_same_folder_module_found():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        write(tmp, "sr_local_helper_zzz.py", "VALUE = 1\n")
        path = write(tmp, "uses.py",
                     "import sr_local_helper_zzz\n"
                     "print(sr_local_helper_zzz.VALUE)\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result
        path = write(tmp, "uses2.py", "import sr_no_such_module_zzz\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_ERROR, result
        assert result.kind == "import"


def test_check_python_cp932_coding_declaration():
    with tempfile.TemporaryDirectory() as td:
        raw = "# -*- coding: cp932 -*-\nprint('あ')\n".encode("cp932")
        path = write(Path(td), "declared.py", raw)
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result


def test_truncate_trailing_newline_and_crlf():
    exact = "\n".join(f"line{i}" for i in range(250))
    # 末尾の改行は行数に数えない(ちょうど250行で中略が入らない)。
    assert core.truncate_text(exact + "\n", 200, 50, 100000) == exact
    # CRLF も \n にそろえて数える。
    crlf = "\r\n".join(f"line{i}" for i in range(250)) + "\r\n"
    assert core.truncate_text(crlf, 200, 50, 100000) == exact
    over = "\n".join(f"line{i}" for i in range(251)) + "\n"
    assert "(中略 1 行)" in core.truncate_text(over, 200, 50, 100000)


def test_build_check_preview_unknown():
    result = core.CheckResult(status=core.STATUS_UNKNOWN,
                              kind="no_powershell")
    preview = core.build_check_preview(result)
    assert "チェックできていません" in preview
    assert "文法エラーはありません" not in preview
    warn = core.CheckResult(status=core.STATUS_WARN, kind="danger")
    assert "文法エラーはありません" in core.build_check_preview(warn)


def test_ps_member_warns_matching():
    methods = [
        {"line": 1, "member": "Delete"},
        {"line": 2, "member": "MoveTo"},
        {"line": 3, "member": "CopyFile"},
        {"line": 4, "member": "CreateDirectory"},
        {"line": 5, "member": "Kill"},
        {"line": 6, "member": "Start"},
        {"line": 7, "member": "InvokeScript"},
        {"line": 8, "member": "WriteAllText"},
        {"line": 9, "member": "SetContent"},
        {"line": 10, "member": "Read"},
        {"line": 11, "member": "ToString"},
        {"line": 12, "member": "StartProcess"},  # Start は完全一致だけ
        {"line": 13, "member": ""},
    ]
    warns = core._ps_member_warns(methods)
    assert [line for line, _msg in warns] == [1, 2, 3, 4, 5, 6, 7, 8, 9], \
        warns
    assert "Delete" in warns[0][1]


def test_ps1_bom_copy_source_cases():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        plain = write(tmp, "a.ps1", "Write-Output 1\n")
        assert core.ps1_bom_copy_source(plain) is None  # ASCII のみ
        bomed = write(tmp, "b.ps1",
                      b"\xef\xbb\xbf" + "Write-Output 'あ'\n".encode("utf-8"))
        assert core.ps1_bom_copy_source(bomed) is None  # BOM 付き
        jp = write(tmp, "c.ps1", "Write-Output 'こんにちは'\n")
        assert core.ps1_bom_copy_source(jp) == jp.read_bytes()
        assert core.ps1_bom_copy_source(tmp / "none.ps1") is None


_PS_STUB_RESULT = {"errors": [], "commands": [], "redirections": [],
                   "statics": [], "dynamics": [], "methods": [],
                   "resolved": {}}


def _write_stub_ps(tmp: Path, payload: dict, record: Path) -> Path:
    """powershell.exe の代わりに置く検査役もどき。

    SCRIPT_RUNNER_TARGET のパスと先頭3バイトを record へ書き、
    payload の JSON を標準出力へ返す。"""
    src = (
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "target = os.environ['SCRIPT_RUNNER_TARGET']\n"
        "data = open(target, 'rb').read()\n"
        f"rec = open({str(record)!r}, 'w', encoding='utf-8')\n"
        "rec.write(target + '\\n' + data[:3].hex())\n"
        "rec.close()\n"
        f"print(json.dumps({payload!r}))\n"
    )
    stub = tmp / "stub_ps.py"
    stub.write_text(src, encoding="utf-8", newline="")
    stub.chmod(0o755)
    return stub


def test_check_powershell_bomless_utf8_uses_copy():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        record = tmp / "record.txt"
        stub = _write_stub_ps(tmp, _PS_STUB_RESULT, record)
        path = write(tmp, "jp.ps1", "Write-Output 'こんにちは'\n")
        result = core.check_powershell_script(
            path, powershell_exe=str(stub))
        assert result.status == core.STATUS_OK, result
        target, head = record.read_text(encoding="utf-8").splitlines()
        assert target != str(path)             # 写しをチェックしている
        assert Path(target).name == "jp.ps1"   # 名前は元のまま
        assert Path(target).parent != tmp      # 一時フォルダ側
        assert head == "efbbbf"                # BOM が付いている
        assert result.sha256 == core.file_sha256(path)  # sha は元のもの
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
        # BOM 付きは元のファイルをそのまま使う。
        bomed = write(tmp, "b.ps1",
                      b"\xef\xbb\xbf" + "Write-Output 'あ'\n".encode("utf-8"))
        result = core.check_powershell_script(
            bomed, powershell_exe=str(stub))
        assert result.status == core.STATUS_OK, result
        target, _head = record.read_text(encoding="utf-8").splitlines()
        assert target == str(bomed)


def test_check_powershell_member_warns_via_stub():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        payload = dict(_PS_STUB_RESULT, methods=[
            {"line": 2, "member": "Delete"},
            {"line": 3, "member": "Read"}])
        stub = _write_stub_ps(tmp, payload, tmp / "record.txt")
        path = write(tmp, "m.ps1",
                     "Write-Output 1\n(Get-Item 'x').Delete()\n")
        result = core.check_powershell_script(
            path, powershell_exe=str(stub))
        assert result.status == core.STATUS_WARN, result
        assert any(issue.line == 2 and "Delete" in issue.message
                   for issue in result.issues), result
        assert all("Read" not in issue.message
                   for issue in result.issues), result


def _write_run_stub(tmp: Path, record: Path) -> Path:
    """powershell.exe の代わりに置く実行もどき。-File の対象パスと
    先頭3バイトを record へ書いて、正常終了する。"""
    src = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "target = sys.argv[sys.argv.index('-File') + 1]\n"
        "data = open(target, 'rb').read()\n"
        f"rec = open({str(record)!r}, 'w', encoding='utf-8')\n"
        "rec.write(target + '\\n' + data[:3].hex())\n"
        "rec.close()\n"
    )
    stub = tmp / "stub_run.py"
    stub.write_text(src, encoding="utf-8", newline="")
    stub.chmod(0o755)
    return stub


def test_execute_powershell_bomless_utf8_uses_copy_and_cleans_up():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        record = tmp / "run_record.txt"
        stub = _write_run_stub(tmp, record)
        path = write(tmp, "run.ps1", "Write-Output 'こんにちは'\n")
        original = core.find_powershell
        core.find_powershell = lambda: str(stub)
        try:
            run = core.execute_script(path, CONFIG)
        finally:
            core.find_powershell = original
        assert run.exit_code == 0, run
        target, head = record.read_text(encoding="utf-8").splitlines()
        copy_path = tmp / (core.BOM_COPY_PREFIX + "run.ps1")
        assert target == str(copy_path)  # 同じフォルダの写しで実行
        assert head == "efbbbf"
        assert not copy_path.exists()   # 終わったら消える
        assert run.sha256 == core.file_sha256(path)
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_execute_powershell_bom_copy_failure_is_encoding_error():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        record = tmp / "run_record.txt"
        stub = _write_run_stub(tmp, record)
        path = write(tmp, "run.ps1", "Write-Output 'こんにちは'\n")
        original_find = core.find_powershell
        original_write_bytes = Path.write_bytes

        def flaky_write_bytes(self, data):
            if self.name.startswith(core.BOM_COPY_PREFIX):
                raise OSError("写しを作れません")
            return original_write_bytes(self, data)

        core.find_powershell = lambda: str(stub)
        Path.write_bytes = flaky_write_bytes
        try:
            run = core.execute_script(path, CONFIG)
        finally:
            core.find_powershell = original_find
            Path.write_bytes = original_write_bytes
        assert run.exit_code is None
        assert "BOM 付きの UTF-8 で保存してください" in run.start_error
        assert not record.exists()  # 実行されていない


def test_execute_expected_sha256():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        marker = tmp / "ran.txt"
        path = write(tmp, "mark.py",
                     f"open({str(marker)!r}, 'w').write('x')\n")
        good = core.file_sha256(path)
        run = core.execute_script(path, CONFIG, expected_sha256="0" * 64)
        assert run.exit_code is None and run.start_error
        assert not marker.exists()
        run = core.execute_script(path, CONFIG, expected_sha256=good)
        assert run.exit_code == 0 and marker.exists()


def test_execute_timeout_keeps_partial_output_and_closes_job():
    original_assign, original_close = core._job_assign, core._job_close
    closed = []
    core._job_assign = lambda proc: "JOB"
    core._job_close = lambda job: closed.append(job)
    try:
        with tempfile.TemporaryDirectory() as td:
            path = write(
                Path(td), "family.py",
                "import subprocess, sys, time\n"
                "print('start', flush=True)\n"
                "subprocess.Popen([sys.executable, '-c',"
                " 'import time; time.sleep(30)'])\n"
                "time.sleep(30)\n")
            config = dict(CONFIG, timeout_seconds=1)
            started = time.monotonic()
            run = core.execute_script(path, config)
            elapsed = time.monotonic() - started
    finally:
        core._job_assign, core._job_close = original_assign, original_close
    assert run.timed_out
    assert closed == ["JOB"], closed       # ジョブを閉じて止めた
    assert "start" in run.stdout           # 部分出力を捨てない
    assert elapsed < 25, elapsed


# ---------------------------------------------------------------
# レビュー req-20261114-14 の追加分
# ---------------------------------------------------------------
def test_check_python_builtin_modules_found():
    # 組み込みモジュールだけの .py が「見つかりません」にならない
    # (PathFinder は組み込みモジュールを見つけられない退行の対策)。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = write(tmp, "builtins.py",
                     "import sys\nimport time\nimport math\n"
                     "import itertools\nprint(1)\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result
        # from 形式と、標準ライブラリの .py モジュールとの混在でも通る。
        path = write(tmp, "mixed.py",
                     "from itertools import chain\nimport json\n"
                     "import sys\nprint(list(chain([1], [2])))\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_OK, result
        # 無いモジュールは相変わらず見つからない。
        path = write(tmp, "missing.py", "import sr_no_such_module_zzz\n")
        result = core.check_python_script(path, PY)
        assert result.status == core.STATUS_ERROR, result
        assert result.kind == "import"


def test_run_handle_stop_closes_job():
    original_close = core._job_close
    closed = []
    core._job_close = lambda job: closed.append(job)
    try:
        handle = core.RunHandle()
        handle._attach_job("JOB")
        handle.stop()
        # 停止でジョブを閉じる(孫プロセスごと終わらせるため)。
        assert closed == ["JOB"], closed
        # もう一度止めても二重には閉じない。
        handle.stop()
        handle._close_job()
        assert closed == ["JOB"], closed
    finally:
        core._job_close = original_close


def test_execute_stop_closes_job_once():
    original_assign, original_close = core._job_assign, core._job_close
    closed = []
    core._job_assign = lambda proc: "JOB"
    core._job_close = lambda job: closed.append(job)
    try:
        with tempfile.TemporaryDirectory() as td:
            path = write(Path(td), "slow.py",
                         "import time\ntime.sleep(60)\n")
            handle = core.RunHandle()
            timer = threading.Timer(0.5, handle.stop)
            timer.start()
            run = core.execute_script(path, CONFIG, handle)
            timer.cancel()
    finally:
        core._job_assign, core._job_close = \
            original_assign, original_close
    assert run.stopped
    assert run.seconds < 20
    # 停止のときに1回だけ閉じ、実行側の後始末で二重に閉じない。
    assert closed == ["JOB"], closed


def test_cleanup_bom_copies():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        write(tmp, core.BOM_COPY_PREFIX + "a.ps1", "Write-Output 1\n")
        write(tmp, core.BOM_COPY_PREFIX + "b.ps1", "Write-Output 1\n")
        write(tmp, "keep.py", "print(1)\n")
        write(tmp, "keep.ps1", "Write-Output 1\n")
        removed = core.cleanup_bom_copies(tmp)
        assert removed == 2, removed
        names = sorted(p.name for p in tmp.iterdir())
        assert names == ["keep.ps1", "keep.py"], names
        # フォルダが無くても落ちない。
        assert core.cleanup_bom_copies(tmp / "no_such_dir") == 0


# ---------------------------------------------------------------
# 入口の再開(script_runner_launch。相手と時計は差し替えて確かめる)
# ---------------------------------------------------------------
def test_launch_restart_dispatch():
    calls = []
    originals = (launch.is_running, launch.send, launch.spawn)
    send_ok = {"value": True}

    def fake_send(payload, timeout_ms=1000):
        calls.append(("send", payload))
        return send_ok["value"]

    def fake_spawn(settings_path=None):
        calls.append(("spawn", settings_path))
        return 4242

    try:
        launch.send = fake_send
        launch.spawn = fake_spawn
        # 動いている＋送れた → 再開を頼むだけ。後継はパネル側が用立てる。
        launch.is_running = lambda: True
        send_ok["value"] = True
        assert launch.restart("s.json") == "sent"
        assert calls == [("send", launch.RESTART_COMMAND)], calls
        # 動いているが送れなかった → 起こし直す(start と同じ落ち方)。
        calls.clear()
        send_ok["value"] = False
        assert launch.restart("s.json") == "spawned"
        assert calls == [("send", launch.RESTART_COMMAND),
                         ("spawn", "s.json")], calls
        # 動いていない → 送らずに起こす。
        calls.clear()
        launch.is_running = lambda: False
        assert launch.restart("s.json") == "spawned"
        assert calls == [("spawn", "s.json")], calls
        # start の分岐は従来どおり(show を送る)。
        calls.clear()
        launch.is_running = lambda: True
        send_ok["value"] = True
        assert launch.start("s.json") == "sent"
        assert calls == [("send", launch.SHOW_COMMAND)], calls
    finally:
        launch.is_running, launch.send, launch.spawn = originals


def test_respawn_when_gone_spawns_once_after_exit():
    state = {"checks": 0, "spawned": 0, "now": 0.0}

    def is_running():
        state["checks"] += 1
        return state["checks"] <= 3

    def spawn():
        state["spawned"] += 1

    def sleep(seconds):
        state["now"] += seconds

    done = launch.respawn_when_gone(
        is_running, spawn, timeout=15.0, interval=0.25,
        sleep_fn=sleep, clock=lambda: state["now"])
    assert done is True
    # 消えるまでは起こさず、消えたら1度だけ。
    assert state["checks"] == 4
    assert state["spawned"] == 1


def test_respawn_when_gone_already_gone_spawns_immediately():
    spawned = []
    done = launch.respawn_when_gone(
        lambda: False, lambda: spawned.append(1), timeout=15.0,
        interval=0.25, sleep_fn=lambda _s: None, clock=lambda: 0.0)
    assert done is True
    assert spawned == [1]


def test_respawn_when_gone_gives_up_on_timeout():
    state = {"spawned": 0, "now": 0.0}

    def sleep(seconds):
        state["now"] += seconds

    done = launch.respawn_when_gone(
        lambda: True,
        lambda: state.__setitem__("spawned", state["spawned"] + 1),
        timeout=1.0, interval=0.25, sleep_fn=sleep,
        clock=lambda: state["now"])
    # 上限を過ぎても消えなければ、起こさずに諦める。
    assert done is False
    assert state["spawned"] == 0


def test_diag_events_roundtrip_and_corrupt():
    """診断の控え: 追加・上限・保存と読み直し・壊れたファイル。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "diag.json"
        events: list = []
        core.diag_append(events, "1件目", at=1000.0)
        core.diag_append(events, " 改行を\n含む 文 ", at=1001.0)
        # 文は1行に畳まれる。
        assert events[1]["text"] == "改行を 含む 文"
        core.save_diag_events(events, path)
        loaded = core.load_diag_events(path)
        assert [e["text"] for e in loaded] == ["1件目", "改行を 含む 文"]
        # 上限を超えたら古い方から落ちる。
        many: list = []
        for i in range(core.DIAG_LIMIT + 5):
            core.diag_append(many, f"記録{i}", at=float(i))
        assert len(many) == core.DIAG_LIMIT
        assert many[0]["text"] == "記録5"
        # 長い文は切られる。
        long_events: list = []
        core.diag_append(long_events, "あ" * 500, at=0.0)
        assert len(long_events[0]["text"]) == core.DIAG_TEXT_LIMIT
        # 壊れたファイル・形の違う中身は空として扱う。
        path.write_text("{壊れている", encoding="utf-8")
        assert core.load_diag_events(path) == []
        path.write_text(json.dumps({"at": 1}), encoding="utf-8")
        assert core.load_diag_events(path) == []
        path.write_text(json.dumps(
            [{"at": 1.0, "text": "よい"}, {"at": "x", "text": "だめ"},
             {"text": "だめ"}, "だめ"]), encoding="utf-8")
        assert [e["text"] for e in core.load_diag_events(path)] == ["よい"]
        # 無いファイルも空。
        assert core.load_diag_events(Path(td) / "none.json") == []


def test_diag_bundle_composition_and_trim():
    """診断の束: 見出し・切替・プロファイル名・状態・直近の記録。"""
    events = [{"at": 1000.0 + i, "text": f"記録{i}"} for i in range(40)]
    bundle = core.build_diag_bundle(
        "1.42",
        {"auto_dl": True, "auto_run": False,
         "auto_answer": True, "auto_send": False},
        "既定",
        ["DL行: なし", "実行中: なし"],
        events, at=2000.0)
    lines = bundle.splitlines()
    assert lines[0].startswith("PHAL診断 Ver1.42 ")
    assert "自動化: DL=入 実行=切 回答=入 送信=切" in lines[1]
    assert lines[2] == "AIプロファイル: 既定"
    assert "- DL行: なし" in lines
    # 記録は直近25件だけ、古い順。
    record_lines = [ln for ln in lines if " 記録" in ln]
    assert len(record_lines) == core.DIAG_BUNDLE_EVENT_COUNT
    assert record_lines[0].endswith("記録15")
    assert record_lines[-1].endswith("記録39")
    assert len(bundle) <= core.DIAG_BUNDLE_LIMIT
    # 1件が長い記録ばかりでも、上限に収めて古い方から落とす。
    heavy = [{"at": float(i), "text": "あ" * 230} for i in range(25)]
    bundle2 = core.build_diag_bundle(
        "1.42", {}, "(未設定)", [], heavy, at=3000.0)
    assert len(bundle2) <= core.DIAG_BUNDLE_LIMIT
    assert bundle2.splitlines()[0].startswith("PHAL診断 Ver1.42 ")
    kept = [ln for ln in bundle2.splitlines() if "あ" in ln]
    assert 0 < len(kept) < 25
    # 記録が無くても束は作れる。
    bundle3 = core.build_diag_bundle("1.42", {}, "", [], [], at=0.0)
    assert "AIプロファイル: (未設定)" in bundle3
    # 診断の自動応答の切替も、自動化の行に出る (1-43)。
    bundle4 = core.build_diag_bundle(
        "1.43", {"auto_diag": True}, "既定", [], [], at=0.0)
    assert "診断=入" in bundle4.splitlines()[1]


def test_diag_auto_state_roundtrip_and_corrupt():
    """診断の自動応答の控え (1-43、1-49 で consumed_key を追加):
    保存と読み直し・壊れたファイル・古い形のファイル。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "diag_auto.json"
        # 無いファイルは「消費した物は無く、送ったことも無い」。
        assert core.load_diag_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_sent_at": 0.0}
        core.save_diag_auto(
            {"consumed_id": "id-1", "consumed_key": "key-1",
             "last_sent_at": 123.5}, path)
        assert core.load_diag_auto(path) == {
            "consumed_id": "id-1", "consumed_key": "key-1",
            "last_sent_at": 123.5}
        # 古い形 (consumed_key が無い) のファイルは、key だけ
        # 空として読める。
        path.write_text(json.dumps(
            {"consumed_id": "id-0", "last_sent_at": 10.0}),
            encoding="utf-8")
        assert core.load_diag_auto(path) == {
            "consumed_id": "id-0", "consumed_key": "",
            "last_sent_at": 10.0}
        # 壊れたファイル・形の違う中身は既定に倒す。
        path.write_text("{壊れている", encoding="utf-8")
        assert core.load_diag_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_sent_at": 0.0}
        path.write_text(json.dumps(
            {"consumed_id": 42, "consumed_key": 7,
             "last_sent_at": "x"}),
            encoding="utf-8")
        assert core.load_diag_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_sent_at": 0.0}
        # 間隔は5分。
        assert core.DIAG_AUTO_COOLDOWN_SEC == 300.0


def test_reset_auto_state_roundtrip_and_corrupt():
    """リセットの合図の控え (1-52): 保存と読み直し・壊れたファ
    イル・古い形のファイル。診断の控えとは別ファイルで、間隔は
    10分。診断の束の自動化の行にも状態が出る。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "reset_auto.json"
        # 無いファイルは「消費した物は無く、開き直したことも無い」。
        assert core.load_reset_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_reset_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        core.save_reset_auto(
            {"consumed_id": "id-1", "consumed_key": "key-1",
             "last_reset_at": 123.5}, path)
        assert core.load_reset_auto(path) == {
            "consumed_id": "id-1", "consumed_key": "key-1",
            "last_reset_at": 123.5,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        # 古い形 (consumed_key が無い) のファイルは、key だけ
        # 空として読める。
        path.write_text(json.dumps(
            {"consumed_id": "id-0", "last_reset_at": 10.0}),
            encoding="utf-8")
        assert core.load_reset_auto(path) == {
            "consumed_id": "id-0", "consumed_key": "",
            "last_reset_at": 10.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        # 壊れたファイル・形の違う中身は既定に倒す。
        path.write_text("{壊れている", encoding="utf-8")
        assert core.load_reset_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_reset_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        path.write_text(json.dumps(
            {"consumed_id": 42, "consumed_key": 7,
             "last_reset_at": "x"}),
            encoding="utf-8")
        assert core.load_reset_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_reset_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        # 間隔は10分。控えのファイル名は診断と別。
        assert core.RESET_AUTO_COOLDOWN_SEC == 600.0
        assert core.RESET_AUTO_FILENAME != core.DIAG_AUTO_FILENAME
    # 診断の束の自動化の行に、リセットの状態も出る。
    on = core.build_diag_bundle(
        "1.52", {"auto_reset": True}, "既定", [], [], at=0.0)
    assert "リセット=入" in on.splitlines()[1]
    off = core.build_diag_bundle(
        "1.52", {}, "既定", [], [], at=0.0)
    assert "リセット=切" in off.splitlines()[1]


def test_rerun_auto_state_roundtrip_and_corrupt():
    """再実行の合図の控え (1.63): 保存と読み直し・壊れたファ
    イル・古い形のファイル。診断・リセットの控えとは別ファ
    イルで、間隔は5分。診断の束の自動化の行にも状態が出る。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rerun_auto.json"
        # 無いファイルは「消費した物は無く、再実行したことも無い」。
        assert core.load_rerun_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_rerun_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        core.save_rerun_auto(
            {"consumed_id": "id-1", "consumed_key": "key-1",
             "last_rerun_at": 123.5}, path)
        assert core.load_rerun_auto(path) == {
            "consumed_id": "id-1", "consumed_key": "key-1",
            "last_rerun_at": 123.5,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        # 古い形 (consumed_key が無い) のファイルは、key だけ
        # 空として読める。
        path.write_text(json.dumps(
            {"consumed_id": "id-0", "last_rerun_at": 10.0}),
            encoding="utf-8")
        assert core.load_rerun_auto(path) == {
            "consumed_id": "id-0", "consumed_key": "",
            "last_rerun_at": 10.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        # 壊れたファイル・形の違う中身は既定に倒す。
        path.write_text("{壊れている", encoding="utf-8")
        assert core.load_rerun_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_rerun_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        path.write_text(json.dumps(
            {"consumed_id": 42, "consumed_key": 7,
             "last_rerun_at": "x"}),
            encoding="utf-8")
        assert core.load_rerun_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_rerun_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        # 間隔は5分。控えのファイル名は診断・リセットと別。
        assert core.RERUN_AUTO_COOLDOWN_SEC == 300.0
        assert core.RERUN_AUTO_FILENAME != core.DIAG_AUTO_FILENAME
        assert core.RERUN_AUTO_FILENAME != core.RESET_AUTO_FILENAME
    # 診断の束の自動化の行に、再実行の状態も出る。
    on = core.build_diag_bundle(
        "1.63", {"auto_rerun": True}, "既定", [], [], at=0.0)
    assert "再実行=入" in on.splitlines()[1]
    off = core.build_diag_bundle(
        "1.63", {}, "既定", [], [], at=0.0)
    assert "再実行=切" in off.splitlines()[1]


def test_settings_report_and_marker_auto_state():
    # 設定の報告 (1.85): 節の値が平文に並び、プロファイルの
    # ZIP 名前変えや添付方式、自動化の切替 (設定・マニュアルを
    # 含む) が出ること。保存した開始プロンプトは字数だけ出る。
    section = {
        "watch_folder": "R:/Downloads",
        "work_folder": "R:/work",
        "dark_mode": True,
        "summary_mode": True, "summary_lines": 50,
        "summary_chars": 4000,
        "timeout_seconds": 120,
        "intro_prompt": "保存した文です",
        "auto_dl": True, "auto_settings": True,
        "ai_profiles": [{"name": "Muse", "target": {
            "label": "Muse", "attach_method": "paste",
            "zip_as_txt": True}}],
        "ai_profile_active": 0,
    }
    text = core.build_settings_report(section, "1.85")
    assert text.startswith("PHAL設定 Ver1.85")
    assert "監視フォルダ: R:/Downloads" in text
    assert "作業フォルダ: R:/work" in text
    assert "ダークモード: 入" in text
    assert "要約モード: 入（50行・4000字" in text
    assert "実行の上限: 120秒" in text
    assert "開始プロンプト: 保存した文を使う（7字）" in text
    assert "DL=入" in text and "設定=入" in text
    assert "マニュアル=切" in text
    assert "Muse（有効）" in text
    assert "添付方式=paste" in text and "ZIP名前変え=入" in text
    empty = core.build_settings_report({}, "1.85")
    assert "（既定のダウンロードフォルダ）" in empty
    assert "AIプロファイル: (未設定)" in empty
    assert "開始プロンプト: 自動生成" in empty
    # 設定・マニュアルの控えは別ファイルで往復すること。
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "settings_auto.json"
        mp = Path(td) / "manual_auto.json"
        core.save_settings_auto(
            {"consumed_id": "s1", "consumed_key": "k1",
             "last_sent_at": 12.0}, sp)
        got = core.load_settings_auto(sp)
        assert got == {"consumed_id": "s1", "consumed_key": "k1",
                       "last_sent_at": 12.0,
                       "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        assert core.load_manual_auto(mp)["consumed_id"] == ""
        core.save_manual_auto({"consumed_id": "m1"}, mp)
        assert core.load_manual_auto(mp)["consumed_id"] == "m1"
        assert core.load_settings_auto(sp)["consumed_id"] == "s1"


def test_resolve_file_request_scope():
    # ファイルリクエストの範囲 (1.88): ダウンロードフォルダと
    # 作業フォルダの配下だけ。相対は両方から探し、絶対は配下
    # のみ。「..」での抜け出しは見つからない扱い。
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        watch = base / "watch"; watch.mkdir()
        work = base / "work"; work.mkdir()
        (watch / "a.log").write_text("a", encoding="utf-8")
        sub = work / "sub"; sub.mkdir()
        (sub / "b.log").write_text("b", encoding="utf-8")
        (base / "secret.txt").write_text("s", encoding="utf-8")
        got, reason = core.resolve_file_request(
            "a.log", str(watch), str(work))
        assert got == (watch / "a.log").resolve() and reason == ""
        got, reason = core.resolve_file_request(
            "sub/b.log", str(watch), str(work))
        assert got == (sub / "b.log").resolve() and reason == ""
        got, reason = core.resolve_file_request(
            str(sub / "b.log"), str(watch), str(work))
        assert got == (sub / "b.log").resolve() and reason == ""
        got, reason = core.resolve_file_request(
            str(base / "secret.txt"), str(watch), str(work))
        assert got is None and reason == "outside"
        got, reason = core.resolve_file_request(
            "../secret.txt", str(watch), str(work))
        assert got is None and reason == "not_found"
        got, reason = core.resolve_file_request(
            "missing.log", str(watch), str(work))
        assert got is None and reason == "not_found"
        got, reason = core.resolve_file_request(
            "a.log", str(watch), None)
        assert got == (watch / "a.log").resolve()
        got, reason = core.resolve_file_request(
            "  ", str(watch), str(work))
        assert got is None and reason == "empty"


def test_find_full_output():
    # 全文リクエストの対象探し (1.91): コマンド ID で始まる
    # .txt のうち更新時刻が最新のものを返す。無い ID・不正な
    # ID は None。控えは別ファイルで往復すること。
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "phal-outputs"; folder.mkdir()
        old = folder / "1-123_20260101-000000_aaaaaaaa.txt"
        old.write_text("old", encoding="utf-8")
        new = folder / "1-123_20260102-000000_bbbbbbbb.txt"
        new.write_text("new", encoding="utf-8")
        other = folder / "1-124_20260103-000000_cccccccc.txt"
        other.write_text("other", encoding="utf-8")
        note = folder / "1-123_20260104-000000_dddddddd.md"
        note.write_text("note", encoding="utf-8")
        now = time.time()
        os.utime(old, (now - 200, now - 200))
        os.utime(new, (now - 100, now - 100))
        os.utime(other, (now, now))
        os.utime(note, (now + 100, now + 100))
        assert core.find_full_output("1-123", folder) == new
        assert core.find_full_output("1-124", folder) == other
        assert core.find_full_output("9-999", folder) is None
        assert core.find_full_output("", folder) is None
        assert core.find_full_output("../1-123", folder) is None
        assert core.find_full_output("1-123/x", folder) is None
        assert core.find_full_output("1-123\\x", folder) is None
        sp = Path(td) / "fulltext_auto.json"
        core.save_fulltext_auto(
            {"consumed_id": "f1", "consumed_key": "k1",
             "last_sent_at": 5.0}, sp)
        assert core.load_fulltext_auto(sp) == {
            "consumed_id": "f1", "consumed_key": "k1",
            "last_sent_at": 5.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        assert core.FULLTEXT_AUTO_COOLDOWN_SEC == 60.0


def test_build_history_text():
    # 実行一覧の文 (1.92、素材は実行ログ 1.94): 記録 (古い順)
    # を新しい順に、時刻・ファイル名・終了コード・秒数・旗の
    # 1行ずつにする。記録が無ければその旨の文。控えは往復。
    assert core.build_history_text([]) \
        == "実行の記録はまだありません。"
    records = [
        {"time": "2026-11-15T16:20:37", "file": "1-201_a.phal",
         "exit_code": 0, "seconds": 1.3},
        {"time": "2026-11-15T16:21:24", "file": "1-202_b.phal",
         "exit_code": 1, "seconds": 0.1, "stopped": True},
    ]
    lines = core.build_history_text(records).splitlines()
    assert lines[0] == "直近の実行一覧（新しい順）"
    assert lines[1] == ("11-15 16:21 1-202_b.phal "
                        "終了コード 1（0.1秒）・停止")
    assert lines[2] == ("11-15 16:20 1-201_a.phal "
                        "終了コード 0（1.3秒）")
    assert len(lines) == 3
    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "run.jsonl"
        lp.write_text(
            json.dumps(records[0], ensure_ascii=False) + "\n"
            + "壊れた行\n"
            + json.dumps(records[1], ensure_ascii=False) + "\n",
            encoding="utf-8")
        got = core.load_run_log_records(20, lp)
        assert [r["file"] for r in got] == [
            "1-201_a.phal", "1-202_b.phal"]
        assert core.load_run_log_records(1, lp)[0]["file"] \
            == "1-202_b.phal"
        sp = Path(td) / "history_auto.json"
        core.save_history_auto(
            {"consumed_id": "h1", "consumed_key": "k1",
             "last_sent_at": 7.0}, sp)
        assert core.load_history_auto(sp) == {
            "consumed_id": "h1", "consumed_key": "k1",
            "last_sent_at": 7.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}


def test_log_request_parse_select_reply():
    # ログリクエスト (1.96): 指定の解釈 (件数・日付・範囲・
    # 合成・丸め・読めない指定は無視)、範囲での選び出し、
    # 返信の見出し。控えは load_log_events と別ファイルで往復。
    from datetime import datetime as _dt
    now = _dt(2026, 11, 15, 17, 0, 0)
    spec = core.parse_log_request("", now=now)
    assert spec["date"] is None and spec["start_min"] is None
    assert spec["count"] == 50 and spec["label"] == "直近50件"
    spec = core.parse_log_request("100", now=now)
    assert spec["count"] == 100 and spec["label"] == "直近100件"
    spec = core.parse_log_request("9999", now=now)
    assert spec["count"] == 500  # 上限に丸める
    spec = core.parse_log_request("0", now=now)
    assert spec["count"] == 1  # 下限に丸める
    spec = core.parse_log_request("2026-11-15", now=now)
    assert str(spec["date"]) == "2026-11-15"
    assert spec["label"] == "2026-11-15"
    spec = core.parse_log_request("16:00-16:30", now=now)
    assert spec["start_min"] == 960 and spec["end_min"] == 990
    assert spec["label"] == "16:00〜16:30"
    spec = core.parse_log_request("16:00-16:30 2026-11-15", now=now)
    assert spec["label"] == "2026-11-15 16:00〜16:30"
    spec = core.parse_log_request("abc 25:00-26:00", now=now)
    assert spec["date"] is None and spec["start_min"] is None
    assert spec["count"] == 50

    def at(y, mo, d, h, mi):
        return _dt(y, mo, d, h, mi).timestamp()

    events = [
        {"at": at(2026, 11, 14, 16, 15), "text": "昨日の範囲内"},
        {"at": at(2026, 11, 15, 15, 59), "text": "範囲の前"},
        {"at": at(2026, 11, 15, 16, 0), "text": "範囲の始まり"},
        {"at": at(2026, 11, 15, 16, 30), "text": "範囲の終わり"},
        {"at": at(2026, 11, 15, 16, 31), "text": "範囲の後"},
    ]
    spec = core.parse_log_request("16:00-16:30", now=now)
    got = core.select_log_events(events, spec, now=now)
    assert [e["text"] for e in got] == ["範囲の始まり", "範囲の終わり"]
    spec = core.parse_log_request("2026-11-14", now=now)
    got = core.select_log_events(events, spec, now=now)
    assert [e["text"] for e in got] == ["昨日の範囲内"]
    spec = core.parse_log_request("2", now=now)
    got = core.select_log_events(events, spec, now=now)
    assert [e["text"] for e in got] == ["範囲の終わり", "範囲の後"]
    spec = core.parse_log_request("16:00-16:30", now=now)
    picked = core.select_log_events(events, spec, now=now)
    lines = core.build_log_reply(picked, spec).splitlines()
    assert lines[0] == "動作ログ（16:00〜16:30・2件）"
    assert len(lines) == 3 and "範囲の始まり" in lines[1]
    spec = core.parse_log_request("", now=now)
    reply = core.build_log_reply(events[-2:], spec)
    assert reply.splitlines()[0] == "動作ログ（直近50件）"
    assert core.build_log_reply([], spec) == \
        "動作ログ（直近50件）\n（記録はありません）"
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "log_auto.json"
        core.save_log_auto(
            {"consumed_id": "l1", "consumed_key": "k1",
             "last_sent_at": 3.0}, sp)
        assert core.load_log_auto(sp) == {
            "consumed_id": "l1", "consumed_key": "k1",
            "last_sent_at": 3.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        assert core.LOG_AUTO_FILENAME \
            == "script_runner_log_auto.json"
        assert core.LOG_AUTO_COOLDOWN_SEC == 60.0


def test_log_events_roundtrip_and_cap():
    # 動作ログ (1.89): 往復と上限 (1000件) と、ビューア用の
    # 行の整形。診断の控え (60件) とは別ファイルで混ざらない。
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "log.json"
        events = []
        core.log_append(events, "一行目", at=1000.0)
        core.log_append(events, "二行目", at=1001.0)
        core.save_log_events(events, path)
        got = core.load_log_events(path)
        assert [e["text"] for e in got] == ["一行目", "二行目"]
        lines = core.format_log_lines(got)
        assert lines[0].endswith(" 一行目") and ":" in lines[0]
        big = []
        for i in range(core.LOG_LIMIT + 5):
            core.log_append(big, f"行{i}", at=float(i))
        assert len(big) == core.LOG_LIMIT
        assert big[0]["text"] == "行5"
        core.save_log_events(big, path)
        assert len(core.load_log_events(path)) == core.LOG_LIMIT
        assert core.load_diag_events(
            Path(td) / "diag.json") == []


def test_log_filenames_do_not_collide():
    # 実行ログ (append_run_log) と動作ログ (1.89) のファイル名
    # 衝突の回帰 (1.93): 1.89 で動作ログ側も LOG_FILENAME を
    # 再定義してしまい、append_run_log が動作ログのファイル
    # (.json) へ JSONL を追記して壊す状態だった。定数は
    # LOG_FILENAME (.jsonl、実行ログ) と ACTIVITY_LOG_FILENAME
    # (.json、動作ログ) に分かれていること。append_run_log の
    # 既定の書き先が .jsonl で、動作ログへは書かないこと。
    assert core.LOG_FILENAME == "script_runner_log.jsonl"
    assert core.ACTIVITY_LOG_FILENAME == "script_runner_log.json"
    assert core.log_path().name == "script_runner_log.json"
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td) / "data"):
            events = []
            core.log_append(events, "動作ログの一行", at=1000.0)
            core.save_log_events(events)
            run = core.RunResult(
                name="a.py", path="C:/x/a.py", exit_code=0,
                seconds=1.0, stdout="出力", sha256="ab12")
            core.append_run_log(run)
            run_log = Path(td) / "data" / core.LOG_FILENAME
            lines = run_log.read_text(
                encoding="utf-8").splitlines()
            assert len(lines) == 1
            assert json.loads(lines[0])["file"] == "a.py"
            # 動作ログは壊れず、そのまま読める。
            got = core.load_log_events()
            assert [e["text"] for e in got] == ["動作ログの一行"]


def test_stop_auto_state():
    """停止リクエストの控え (1.93): 保存と読み直し・壊れた
    ファイル。間隔は60秒、ファイルは他の合図と別。診断の束と
    設定の報告の自動化の行にも状態が出る。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "stop_auto.json"
        assert core.load_stop_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_sent_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        core.save_stop_auto(
            {"consumed_id": "s-1", "consumed_key": "k-1",
             "last_sent_at": 123.0,
             "last_payload": "p-1", "last_answered_at": 42.0}, path)
        assert core.load_stop_auto(path) == {
            "consumed_id": "s-1", "consumed_key": "k-1",
            "last_sent_at": 123.0,
            "last_payload": "p-1", "last_answered_at": 42.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        assert core.STOP_AUTO_COOLDOWN_SEC == 60.0
        assert core.STOP_AUTO_FILENAME \
            == "script_runner_stop_auto.json"
        bundle = core.build_diag_bundle(
            "1.93", {"auto_stop": True}, "phal-ai", [], [])
        assert "停止=入" in bundle
        report = core.build_settings_report(
            {"auto_stop": True}, "1.93")
        assert "停止=入" in report


def test_resend_auto_state_and_text():
    """結果再送の合図の控えと再送文 (1.75): 保存と読み直し・
    壊れたファイル。間隔は60秒、ファイルは他の合図と別。診
    断の束の自動化の行にも状態が出る。再送文は見出しを付け
    直し、末尾に AI への指示文が付く。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "resend_auto.json"
        assert core.load_resend_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_sent_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        core.save_resend_auto(
            {"consumed_id": "id-1", "consumed_key": "key-1",
             "last_sent_at": 55.5}, path)
        assert core.load_resend_auto(path) == {
            "consumed_id": "id-1", "consumed_key": "key-1",
            "last_sent_at": 55.5,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        path.write_text("{壊れている", encoding="utf-8")
        assert core.load_resend_auto(path) == {
            "consumed_id": "", "consumed_key": "",
            "last_sent_at": 0.0,
            "last_payload": "", "last_answered_at": 0.0,
            "refire_notice_payload": "", "refire_notice_at": 0.0}
        assert core.RESEND_AUTO_COOLDOWN_SEC == 60.0
        assert core.RESEND_AUTO_FILENAME != core.RERUN_AUTO_FILENAME
    on = core.build_diag_bundle(
        "1.75", {"auto_resend": True}, "既定", [], [], at=0.0)
    assert "再送=入" in on.splitlines()[1]
    text = core.build_resend_text("1-100", "--- 出力 ---\nhello")
    assert text.startswith("[実行結果] 1-100 （前回の結果の再送）")
    assert "hello" in text
    assert core.RESULT_AI_PROMPT in text
    cut = core.build_resend_text("1-100", "x", truncated=True)
    assert "打ち切られています" in cut.splitlines()[0]


def test_auto_run_in_scope():
    """自動実行の対象範囲 (案3、1.63): 対象は (a) この PHAL が
    自動で落としたファイルか、(b) ファイル名にコマンド ID が
    付いたファイルだけ。それ以外は範囲外。"""
    # (b) ID つきは、自動で落としていなくても対象。
    assert core.auto_run_in_scope("1-123_job.py", False) is True
    assert core.auto_run_in_scope("12-03_check_db.py", False) is True
    # (a) 自動で落とした分は、ID が無くても対象。
    assert core.auto_run_in_scope("plain.py", True) is True
    assert core.auto_run_in_scope("1-123_job.py", True) is True
    # どちらでもない分は対象外。
    assert core.auto_run_in_scope("plain.py", False) is False
    assert core.auto_run_in_scope("notes.py", False) is False
    # 日付で始まる名前は ID と取り違えない。
    assert core.auto_run_in_scope(
        "2026-11-14_notes.py", False) is False
    # 区切りが "_" でないものは ID とみなさない。
    assert core.auto_run_in_scope("1-123.py", False) is False


def test_stage_color_ratio_and_color():
    """段階表示の色 (1.63、案D): 物差しは段階ごと (実行は
    2分→10分、他は30秒→3分)。黄ばみ始めまでは 0、赤で 1、
    間は線形。色は緑 → 黄 → 赤。待機 (段階なし) や知らない
    段階では進まない。"""
    # 実行の物差し。
    assert core.stage_color_ratio("run", 0) == 0.0
    assert core.stage_color_ratio("run", 119.9) == 0.0
    assert core.stage_color_ratio("run", 120) == 0.0
    assert core.stage_color_ratio("run", 360) == 0.5
    assert core.stage_color_ratio("run", 600) == 1.0
    assert core.stage_color_ratio("run", 3600) == 1.0
    # DL・回答・送信の物差し。
    for stage in ("dl", "answer", "send"):
        assert core.stage_color_ratio(stage, 30) == 0.0
        assert core.stage_color_ratio(stage, 105) == 0.5
        assert core.stage_color_ratio(stage, 180) == 1.0
    # 知らない段階・負の経過は 0.0。
    assert core.stage_color_ratio("bogus", 9999) == 0.0
    assert core.stage_color_ratio(None, 9999) == 0.0
    assert core.stage_color_ratio("run", -5) == 0.0
    # 色の両端と中間。
    assert core.stage_color("run", 0) == (40, 167, 69)
    assert core.stage_color("run", 360) == (255, 193, 7)
    assert core.stage_color("run", 600) == (220, 53, 69)
    # 緑 → 黄の途中は両成分が混ざる。
    red, green, blue = core.stage_color("dl", 67.5)
    assert 40 < red < 255 and 167 < green < 193


def test_parse_progress_line():
    """進捗申告の行 (1.63): 「進捗：N/M」だけを拾う。"""
    assert core.parse_progress_line("進捗：3/10") == (3, 10)
    assert core.parse_progress_line("進捗: 3/10") == (3, 10)
    assert core.parse_progress_line(" 進捗：0/5 ") == (0, 5)
    assert core.parse_progress_line("進捗：3/0") is None
    assert core.parse_progress_line("進捗：3/10 追加の文") is None
    assert core.parse_progress_line("処理中：3/10") is None
    assert core.parse_progress_line("普通の出力") is None
    assert core.parse_progress_line("") is None
    assert core.parse_progress_line(None) is None


def test_execute_streaming_output():
    """逐次取り込み (1.63): on_output へ行が順に届き、戻りの
    stdout は全文のまま。持ち手 (handle.on_output) 経由でも
    同じく届く。進捗申告の行も出力の1行として届く。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = write(
            tmp, "stream.py",
            "import sys\n"
            "print('1行目')\n"
            "print('進捗：1/2')\n"
            "sys.stderr.write('エラー側\\n')\n"
            "print('2行目')\n")
        seen: list = []
        run = core.execute_script(
            path, CONFIG,
            on_output=lambda kind, line: seen.append((kind, line)))
        assert run.exit_code == 0
        assert ("stdout", "1行目") in seen
        assert ("stdout", "進捗：1/2") in seen
        assert ("stderr", "エラー側") in seen
        assert ("stdout", "2行目") in seen
        # 標準出力の行は順序どおり。
        out_lines = [line for kind, line in seen if kind == "stdout"]
        assert out_lines == ["1行目", "進捗：1/2", "2行目"], out_lines
        # 戻りの全文は従来どおり (進捗の行も残る)。
        assert "1行目" in run.stdout and "2行目" in run.stdout
        assert "進捗：1/2" in run.stdout
        assert "エラー側" in run.stderr
        # 持ち手経由でも届く (窓の経路)。
        seen2: list = []
        handle = core.RunHandle()
        handle.on_output = lambda kind, line: seen2.append(
            (kind, line))
        run = core.execute_script(path, CONFIG, handle)
        assert run.exit_code == 0
        assert ("stdout", "1行目") in seen2


def test_phal_status_build_and_write_only_on_change():
    """モニタ表示用の状態 (req-20261115-02): 形・変化時だけ書く・
    原子的な置き換え・失敗しても例外を出さない。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "phal_status.json"
        status = core.build_phal_status(
            "1.46", "run", command_id="12-03",
            detail="実行中: 12-03_demo.py",
            step=2, item="デモをする", at=1700000000)
        assert status == {
            "schema": 1, "version": "1.46", "state": "run",
            "command_id": "12-03",
            "detail": "実行中: 12-03_demo.py",
            "stopped_reason": "", "step": 2, "item": "デモをする",
            "updated": 1700000000}
        # 知らない state は作らない。
        try:
            core.build_phal_status("1.46", "flying")
        except ValueError:
            pass
        else:
            raise AssertionError("unknown state did not raise")
        # detail / item は1行に畳まれる。
        folded = core.build_phal_status(
            "1.46", "idle", detail=" 待ち\n  受け ", item="a\nb", at=1)
        assert folded["detail"] == "待ち 受け"
        assert folded["item"] == "a b"
        # 初回は書き、同じ内容(時刻だけ違う)は書かない。
        assert core.write_phal_status(status, path) is True
        assert json.loads(path.read_text(encoding="utf-8")) == status
        assert not (path.with_name(path.name + ".tmp")).exists()
        same = core.build_phal_status(
            "1.46", "run", command_id="12-03",
            detail="実行中: 12-03_demo.py",
            step=2, item="デモをする", at=1700000001)
        mtime = path.stat().st_mtime_ns
        assert core.write_phal_status(same, path) is False
        assert path.stat().st_mtime_ns == mtime
        assert json.loads(path.read_text(encoding="utf-8"))["updated"] \
            == 1700000000
        # 変われば書く(時刻も新しくなる)。
        changed = core.build_phal_status(
            "1.46", "idle", command_id="12-03", detail="実行が終わりました",
            step=2, item="デモをする", at=1700000002)
        assert core.write_phal_status(changed, path) is True
        assert json.loads(path.read_text(encoding="utf-8")) == changed
        assert not (path.with_name(path.name + ".tmp")).exists()
        # 壊れた前回ファイルは書き直す。
        path.write_text("{壊れている", encoding="utf-8")
        assert core.write_phal_status(changed, path) is True
        assert json.loads(path.read_text(encoding="utf-8")) == changed
        # 書けない場所でも例外を出さず False。
        blocker = Path(td) / "blocker"
        blocker.write_text("file, not dir", encoding="utf-8")
        assert core.write_phal_status(
            status, blocker / "phal_status.json") is False
        # 置き場は data_dir() の下。
        assert core.phal_status_path() == \
            core.data_dir() / "phal_status.json"


def test_format_elapsed():
    """窓の上の帯 (1.48) の経過・所要時間の形。「分:秒」、1時間
    以上は「時:分:秒」、まだ実行していなければ「--:--」。"""
    assert core.format_elapsed(None) == "--:--"
    assert core.format_elapsed(0) == "00:00"
    assert core.format_elapsed(5) == "00:05"
    assert core.format_elapsed(65) == "01:05"
    assert core.format_elapsed(125.9) == "02:05"
    assert core.format_elapsed(3599) == "59:59"
    assert core.format_elapsed(3600) == "1:00:00"
    assert core.format_elapsed(3723) == "1:02:03"
    assert core.format_elapsed(-3) == "00:00"


# ---------------------------------------------------------------
# 導入プロンプト (1.47)
# ---------------------------------------------------------------
def _fake_intro_runner(cmd, timeout=10):
    """collect_intro_environment 用の差し替え実行役。

    python --version と PowerShell の版・実行ポリシーを、決まった
    文で返す。PowerShell の実行ポリシーは、拾う対象の2行の他に
    MachinePolicy / UserPolicy の行も混ぜてある。"""
    joined = " ".join(cmd)
    if "--version" in joined:
        return "Python 3.11.9\n"
    if "PSVersion" in joined:
        return "5.1.19041.1\n"
    if "ExecutionPolicy" in joined:
        return ("MachinePolicy=Undefined\n"
                "UserPolicy=Undefined\n"
                "Process=Undefined\n"
                "CurrentUser=Undefined\n"
                "LocalMachine=RemoteSigned\n")
    raise AssertionError(f"unexpected command: {cmd!r}")


def test_collect_intro_environment_with_fake_runner():
    env = core.collect_intro_environment(
        runner=_fake_intro_runner, watch_folder="/tmp/dl",
        python_exe="python")
    assert env["python_version"] == "3.11.9", env
    assert env["powershell_version"] == "5.1.19041.1", env
    # 実行ポリシーは LocalMachine と CurrentUser の要点だけ。
    assert env["execution_policy"] == \
        "LocalMachine=RemoteSigned, CurrentUser=Undefined", env
    assert env["watch_folder"] == "/tmp/dl", env
    assert env["encoding"] != core.INTRO_UNKNOWN, env
    # モジュール一覧はこの環境の実際の配布物 (中身は問わない)。
    assert env["modules"] is None or isinstance(env["modules"], list)
    assert isinstance(env["modules_more"], int)


def test_collect_intro_environment_runner_missing_is_unknown():
    # 実行役が何も返せない (PowerShell が無い VM など) 場合は、
    # その項目は「不明」になり、ボタンは止まらない。
    def missing(cmd, timeout=10):
        raise FileNotFoundError("powershell")

    env = core.collect_intro_environment(
        runner=missing, watch_folder="/tmp/dl", python_exe="python")
    assert env["python_version"] == core.INTRO_UNKNOWN, env
    assert env["powershell_version"] == core.INTRO_UNKNOWN, env
    assert env["execution_policy"] == core.INTRO_UNKNOWN, env
    assert env["watch_folder"] == "/tmp/dl", env


def test_collect_intro_modules_truncated():
    # 20件あれば先頭12件だけ残し、残りは「他 N 件」の数になる。
    import unittest.mock as mock

    class _FakeDist:
        def __init__(self, name):
            self.metadata = {"Name": name}
            self.version = "1.0"

    dists = [_FakeDist(f"pkg{i:02d}") for i in range(20)]
    with mock.patch("importlib.metadata.distributions",
                    return_value=dists):
        modules, more = core._intro_modules()
    assert len(modules) == core.INTRO_MODULE_LIMIT, modules
    assert modules[0] == "pkg00 1.0"
    assert more == 20 - core.INTRO_MODULE_LIMIT, more


def test_zip_as_txt_paths():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        z = folder / "data.zip"
        z.write_bytes(b"PK-dummy")
        t = folder / "note.txt"
        t.write_text("x", encoding="utf-8")
        paths, renamed = core.zip_as_txt_paths([z, t])
        assert renamed == [("data.zip", "data.zip.txt")], renamed
        assert paths[0].name == "data.zip.txt"
        assert paths[0].read_bytes() == b"PK-dummy"
        assert paths[1] == t
        # ZIP でないものだけなら何も起きない
        paths2, renamed2 = core.zip_as_txt_paths([t])
        assert paths2 == [t] and renamed2 == []


def test_build_intro_prompt():
    env = {
        "python_version": "3.11.9",
        "powershell_version": "5.1.19041.1",
        "os_version": "Windows 11",
        "execution_policy": "LocalMachine=RemoteSigned",
        "watch_folder": r"C:\Users\yotan\Downloads",
        "encoding": "cp932",
        "modules": [f"pkg{i:02d} 1.0" for i in range(12)],
        "modules_more": 2,
    }
    text = core.build_intro_prompt(env)
    assert text.startswith("PHAL のループを始めます。"), text
    for needle in ("3.11.9", "5.1.19041.1", "Windows 11",
                   "LocalMachine=RemoteSigned",
                   r"C:\Users\yotan\Downloads", "cp932",
                   "直接出られません",
                   "回答は短いスクリプトを .phal ファイルで返してください。"):
        assert needle in text, text
    # モジュールの記載は 1.82 で廃止 (後半が省略され意味が無い)。
    assert "pkg00 1.0" not in text, text
    # マニュアルの添付は、env の印があるときだけ文に出る (1.78)。
    assert "添付しています" not in text, text
    # ZIP の行は、プロファイルで有効な相手にだけ出る (1.81)。
    assert ".zip.txt" not in text, text
    assert "開始プロンプト第7版" in text, text
    # 作業フォルダの行は、選んだフォルダがあるときだけ出る (1.83)。
    assert "- 作業フォルダ:" not in text, text
    with_work = core.build_intro_prompt(
        {**env, "work_folder": r"D:\work\job1"})
    assert "- 作業フォルダ: D:\\work\\job1" in with_work, with_work
    assert "- モジュール:" not in text, text
    assert "- ダウンロードフォルダ（監視対象）:" in text, text
    zipped = core.build_intro_prompt({**env, "zip_as_txt": True})
    assert ".zip.txt に変えて渡します" in zipped, zipped
    assert ".zip のままで構いません" in zipped, zipped
    attached = core.build_intro_prompt({**env, "manual_attached": True})
    assert "phal-ai-manual.md" in attached, attached
    assert "最初に読んでください" in attached, attached


def test_build_intro_prompt_unknowns():
    # 欠けたキーは「不明」になり、モジュールが読めなければ同じく
    # 「不明」。空の辞書でも文は作れる。
    text = core.build_intro_prompt({})
    assert "PHAL のループを始めます。" in text
    assert text.count(core.INTRO_UNKNOWN) >= 6, text
    assert "- モジュール:" not in text, text
    assert "直接出られません" in text, text


def test_build_intro_prompt_mentions_phal_lib():
    # 導入プロンプトに phal_lib の案内が入っていること。
    text = core.build_intro_prompt({})
    assert "phal_lib" in text, text
    assert "`import phal_lib`" in text, text
    assert "解析用モジュール phal_lib" in text, text


def test_build_ack_text():
    # 受領応答 (1-51): ID があれば「受領：{id} {ファイル名} の
    # 実行を始めます」、無ければ ID の部分を省く。
    assert core.build_ack_text("1-23", "1-23_install.phal") == \
        "受領：1-23 1-23_install.phal の実行を始めます"
    assert core.build_ack_text("", "loop_test.phal") == \
        "受領：loop_test.phal の実行を始めます"
    assert core.build_ack_text(None, "loop_test.phal") == \
        "受領：loop_test.phal の実行を始めます"
    # 診断の束の自動化の行にも、受領の切替が出る。
    bundle = core.build_diag_bundle(
        "1.51", {"auto_ack": True}, "既定", [], [], at=0.0)
    assert "受領=入" in bundle.splitlines()[1]
    bundle_off = core.build_diag_bundle(
        "1.51", {}, "既定", [], [], at=0.0)
    assert "受領=切" in bundle_off.splitlines()[1]


def test_build_blocked_text():
    # チェックで止まった知らせ (1-53): 「止まりました：{id}
    # {ファイル名} はチェックで実行不可（{理由}）」。ID が無け
    # れば ID の部分を、理由が無ければ括弧の部分を省く。
    assert core.build_blocked_text(
        "1-80", "1-80_verify.phal", "モジュール 'phal_lib' が見つかりません") == \
        "止まりました：1-80 1-80_verify.phal はチェックで実行不可" \
        "（モジュール 'phal_lib' が見つかりません）"
    assert core.build_blocked_text("", "loop_test.phal", "文法エラー 1件") == \
        "止まりました：loop_test.phal はチェックで実行不可（文法エラー 1件）"
    assert core.build_blocked_text("1-80", "1-80_verify.phal", "") == \
        "止まりました：1-80 1-80_verify.phal はチェックで実行不可"


def test_blocked_notified_roundtrip_and_corrupt():
    """止まった知らせの控え (1-53): 保存と読み直し・期限切れ・
    壊れたファイル。実行済みの控えとは別ファイル。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "blocked.json"
        # 無いファイルは空。
        assert core.load_blocked_notified(path) == {}
        records = core.load_blocked_notified(path)
        core.blocked_notified_put(records, "sha-a")
        core.save_blocked_notified(records, path)
        loaded = core.load_blocked_notified(path)
        assert core.blocked_notified_contains(loaded, "sha-a")
        assert not core.blocked_notified_contains(loaded, "sha-b")
        assert not core.blocked_notified_contains(loaded, "")
        # 期限 (7日) を超えた控えは、読み込みで落ちて含まない。
        stale = {"sha-old": time.time() - 8 * 24 * 3600}
        core.save_blocked_notified(stale, path)
        assert core.load_blocked_notified(path) == {}
        assert not core.blocked_notified_contains(stale, "sha-old")
        # 壊れたファイル・形の違う中身は空に倒す。
        path.write_text("{壊れている", encoding="utf-8")
        assert core.load_blocked_notified(path) == {}
        path.write_text(json.dumps(["sha-a"]), encoding="utf-8")
        assert core.load_blocked_notified(path) == {}
        # 実行済みの控えとは別ファイル。
        assert core.BLOCKED_NOTIFIED_FILENAME != \
            core.EXECUTED_HASH_FILENAME


def test_extract_attach_lines():
    # 添付の指定 (1.58): 「添付：ファイル名」の行を取り出し、
    # (残りのテキスト, 名前の並び) を返す。全角・半角のコロン
    # どちらでもよく、行の前後の空白は許す。
    text = "集計しました\n添付：result.csv\n添付: notes.txt\n以上です"
    cleaned, names = core.extract_attach_lines(text)
    assert names == ["result.csv", "notes.txt"]
    assert cleaned == "集計しました\n以上です"
    # 添付行が無ければ、テキストはそのまま、名前は空。
    cleaned, names = core.extract_attach_lines("普通の出力\n2行目")
    assert cleaned == "普通の出力\n2行目"
    assert names == []
    # 空の出力・None でも落ちない。
    assert core.extract_attach_lines("") == ("", [])
    assert core.extract_attach_lines(None) == ("", [])
    # 前後の空白と、名前に付いた空白は落とす。
    cleaned, names = core.extract_attach_lines("  添付：  report.zip  ")
    assert names == ["report.zip"]
    assert cleaned == ""
    # 同じ名前が複数あれば、先の1回だけ数える (行は全部除く)。
    cleaned, names = core.extract_attach_lines(
        "添付：a.txt\n添付：a.txt\n残り")
    assert names == ["a.txt"]
    assert cleaned == "残り"
    # 「添付」を含むだけの普通の行や、行頭でない指定は、
    # 添付の指定とはみなさない。
    cleaned, names = core.extract_attach_lines(
        "添付ファイルは3件でした\n結果に添付：x.txt と書いた")
    assert names == []
    assert cleaned == "添付ファイルは3件でした\n結果に添付：x.txt と書いた"


def test_extract_attach_lines_rejects_paths():
    # フォルダ区切りを含む名前は受け付けない (行は取り除くが、
    # 名前の並びには入れない)。「.」「..」も同じ。
    text = ("添付：sub/a.txt\n添付：..\\secret.txt\n添付：..\n"
            "添付：.\n添付：ok.txt\n本文")
    cleaned, names = core.extract_attach_lines(text)
    assert names == ["ok.txt"]
    assert cleaned == "本文"


def test_attach_file_paths_and_outbox():
    # outbox は data_dir() の下の phal-outbox。実在するファイル
    # だけがパスになり、無いものは「無かった名前」へ分かれる。
    with tempfile.TemporaryDirectory() as td:
        with _temp_data_dir(Path(td)):
            assert core.outbox_dir() == Path(td) / "phal-outbox"
            folder = core.outbox_dir()
            folder.mkdir(parents=True)
            (folder / "a.csv").write_text("x", encoding="utf-8")
            paths, missing = core.attach_file_paths(
                ["a.csv", "b.csv"])
            assert paths == [str(folder / "a.csv")]
            assert missing == ["b.csv"]
            # 名前が無ければどちらも空。
            assert core.attach_file_paths([]) == ([], [])
            assert core.attach_file_paths(None) == ([], [])


def test_split_command_args():
    # 引数文字列の分割 (1.97 / 1.98): 引用符で囲まれた空白は
    # 1語になり、Windows のパス (バックスラッシュ) は壊れない。
    assert core.split_command_args("") == []
    assert core.split_command_args("--lines 20") == ["--lines", "20"]
    assert core.split_command_args(
        '-r "C:\\a b\\f.pcap" -Y sctp') == \
        ["-r", "C:\\a b\\f.pcap", "-Y", "sctp"]
    assert core.split_command_args("R:\\Downloads\\x.pcap") == \
        ["R:\\Downloads\\x.pcap"]
    try:
        core.split_command_args('"閉じていない')
        raise AssertionError("ValueError が出るはず")
    except ValueError:
        pass


def test_execute_script_with_args():
    # 引数つき実行 (1.97): スクリプトの sys.argv[1:] に入る。
    with tempfile.TemporaryDirectory() as td:
        path = write(Path(td), "args.py",
                     "import sys\nprint(sys.argv[1:])\n")
        run = core.execute_script(path, CONFIG,
                                  args=["a b", "x"])
        assert run.exit_code == 0, run.stderr
        assert "['a b', 'x']" in run.stdout


def test_cli_tools_sanitize_and_resolve():
    # ツール実行の許可リスト (1.98): 既定・整形・許可外・実在。
    assert core.sanitize_cli_tools(None) == \
        list(core.DEFAULT_CLI_TOOLS)
    assert core.sanitize_cli_tools("tshark") == \
        list(core.DEFAULT_CLI_TOOLS)  # 並びでなければ既定
    assert core.sanitize_cli_tools(
        ["TShark.EXE", "tshark", "", "ipconfig"]) == \
        ["tshark", "ipconfig"]
    assert core.sanitize_cli_tools([]) == []  # 空は「何も許可しない」
    path, why = core.resolve_cli_tool("evil", ["tshark"])
    assert path is None and why == "not_allowed"
    # 2.01: 実行時は登録パスが正で、許可リストにあるだけでは
    # 探しに行かない (no_path)。探すのは設定の「探す」の役割。
    stem = core.normalize_tool_name(Path(PY).name)
    path, why = core.resolve_cli_tool(stem, [stem])
    assert path is None and why == "no_path"
    assert core.search_cli_tool(stem) is not None
    path, why = core.resolve_cli_tool(
        "definitely-not-a-real-tool-xyz", ["definitely-not-a-real-tool-xyz"])
    assert path is None and why == "no_path"
    assert core.search_cli_tool(
        "definitely-not-a-real-tool-xyz") is None


def test_cli_tool_registry():
    # ツール登録 (2.00): 名前→パスの登録、使い方、ツール
    # 置き場、Python ツールの実行コマンド。
    assert core.sanitize_cli_tool_paths(None) == {}
    assert core.sanitize_cli_tool_paths(
        {"TShark.EXE": " C:\\x\\tshark.exe ", "bad": ""}) == \
        {"tshark": "C:\\x\\tshark.exe"}
    assert core.sanitize_cli_tool_usages(None) == {}
    assert core.sanitize_cli_tool_usages(
        {"MyTool": "  引数はファイル名  ", "bad": " "}) == \
        {"mytool": "引数はファイル名"}
    # .py のツールは Python で実行する形になる。
    command = core.tool_command(Path("D:/tools/mytool.py"))
    assert command[1] == "D:/tools/mytool.py"
    assert command[0].lower().endswith(
        ("python", "python.exe", "python3", "python3.exe"))
    assert core.tool_command(Path("D:/tools/mytool.exe")) == \
        ["D:/tools/mytool.exe"]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exe = tmp / "mytool.exe"
        exe.write_bytes(b"MZ")
        # 登録のパスが実在すれば、許可リストに無くても
        # 解決する。
        path, why = core.resolve_cli_tool(
            "mytool", [], {"mytool": str(exe)})
        assert path == exe and why == ""
        # 登録のパスが無ければ、別の実体を探しに行かず
        # not_found (黙って別の物を実行しない)。
        path, why = core.resolve_cli_tool(
            "mytool", ["mytool"], {"mytool": str(tmp / "gone.exe")})
        assert path is None and why == "not_found"
        # ツール置き場の実行ファイルは、許可リストに無くても
        # 名前で解決する (.py も対象)。
        placed = tmp / "placed.exe"
        placed.write_bytes(b"MZ")
        path, why = core.resolve_cli_tool(
            "placed", [], None, [tmp])
        assert path == placed and why == ""
        pytool = tmp / "pytool.py"
        pytool.write_text("print(1)\n", encoding="utf-8")
        path, why = core.resolve_cli_tool(
            "pytool", [], None, [tmp])
        assert path == pytool and why == ""
        path, why = core.resolve_cli_tool("other", [], None, [tmp])
        assert path is None and why == "not_allowed"
        # 許可リストにあるだけでパスが未設定なら、実行時は
        # 探しに行かず no_path (2.01)。探すのは設定の役割で、
        # search_cli_tool が担う。
        path, why = core.resolve_cli_tool(
            "unregistered", ["unregistered"], None, [tmp])
        assert path is None and why == "no_path"
        seek = tmp / "seek.exe"
        seek.write_bytes(b"MZ")
        assert core.search_cli_tool("seek", [tmp]) == seek
        assert core.search_cli_tool("other", [tmp]) is None
    note = core.tool_not_found_note("mytool", {"mytool": "D:\\x.exe"})
    assert "D:\\x.exe" in note


def test_execute_command_and_result_text():
    # ツール実行 (1.98): 任意のコマンドを1回実行し、結果文を
    # スクリプトと同じ形に組み立てる。
    run = core.execute_command(
        [PY, "-c", "print('tool-hi')"], timeout_seconds=30)
    assert run.exit_code == 0, run.stderr
    assert "tool-hi" in run.stdout
    text = core.build_tool_result_text(
        "python -c print", run, CONFIG)
    assert text.startswith("[ツール実行結果] python -c print")
    assert "終了コード 0" in text and "tool-hi" in text
    bad = core.execute_command(
        [PY, "-c", "import sys; sys.exit(3)"], timeout_seconds=30)
    assert bad.exit_code == 3
    missing = core.execute_command(
        ["definitely-not-a-real-tool-xyz"], timeout_seconds=30)
    assert missing.start_error  # 起動できないことが理由つきで返る


def test_tool_auto_state_roundtrip():
    # ツール実行の控え (1.98) は他の合図と別ファイルで往復する。
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "tool_auto.json"
        core.save_tool_auto(
            {"consumed_id": "t1", "consumed_key": "k1",
             "last_sent_at": 12.0, "last_payload": "tshark -v",
             "last_answered_at": 13.0}, sp)
        got = core.load_tool_auto(sp)
        assert got["consumed_id"] == "t1"
        assert got["last_payload"] == "tshark -v"
        assert core.TOOL_AUTO_FILENAME == \
            "script_runner_tool_auto.json"


def test_parse_file_request_specs():
    # ファイルリクエストの範囲指定 (1.99): 末尾の指定だけを
    # 切り離し、パス (空白を含む) は残す。bytes が優先。
    path, spec = core.parse_file_request("sub/data.log")
    assert path == "sub/data.log" and spec is None
    path, spec = core.parse_file_request("my logs/a.log lines:3-5")
    assert path == "my logs/a.log"
    assert spec == {"kind": "lines", "start": 3, "end": 5}
    path, spec = core.parse_file_request("a.pcap bytes:100-200")
    assert path == "a.pcap"
    assert spec == {"kind": "bytes", "start": 100, "end": 200}
    path, spec = core.parse_file_request("a.pcap bytes:100+50")
    assert spec == {"kind": "bytes", "start": 100, "end": 150}
    path, spec = core.parse_file_request("a.log lines:7-")
    assert spec == {"kind": "lines", "start": 7, "end": None}
    path, spec = core.parse_file_request(
        "a.pcap lines:1-2 bytes:0-10")
    assert spec["kind"] == "bytes"  # 両方あれば bytes
    path, spec = core.parse_file_request("lines:1-2")
    assert path == "" and spec["kind"] == "lines"


def test_file_range_slicing():
    # 範囲の切り出し (1.99): バイト範囲・行の範囲・文字コード
    # 判定・バイナリ判定・送付ファイル名。
    with tempfile.TemporaryDirectory() as td:
        binpath = Path(td) / "data.bin"
        binpath.write_bytes(bytes(range(256)))
        data, truncated = core.read_byte_range(binpath, 10, 20)
        assert data == bytes(range(10, 20)) and not truncated
        data, truncated = core.read_byte_range(binpath, 200, 9999)
        assert data == bytes(range(200, 256))  # 末尾で丸める
        assert core.looks_binary(bytes(range(256)))
        text = "\n".join(f"行{i}" for i in range(1, 11))
        sliced, total = core.slice_text_lines(
            text, {"kind": "lines", "start": 3, "end": 5})
        assert total == 10 and sliced == "行3\n行4\n行5"
        sliced, _ = core.slice_text_lines(
            text, {"kind": "lines", "start": 9, "end": None})
        assert sliced == "行9\n行10"
        sliced, _ = core.slice_text_lines(
            text, {"kind": "lines", "start": 99, "end": None})
        assert sliced == ""
        decoded, enc = core.decode_text_bytes("あ".encode("cp932"))
        assert decoded == "あ" and enc == "cp932"
        decoded, enc = core.decode_text_bytes(
            "あ".encode("utf-16"))
        assert decoded == "あ" and enc == "utf-16"
        assert not core.looks_binary("ただの文".encode("utf-8"))
        assert core.slice_file_name(
            binpath, {"kind": "bytes", "start": 0, "end": 10}) == \
            "data_bytes0-10.bin"
        assert core.slice_file_name(
            Path("a.log"), {"kind": "lines", "start": 3,
                            "end": None}) == "a_lines3-end.log"


if __name__ == "__main__":
    tests = [
        test_normalize_config_defaults_and_clamps,
        test_list_candidates_filters_and_orders,
        test_file_sha256_changes_with_content,
        test_check_python_ok,
        test_check_python_unclosed_paren,
        test_check_python_indent_error,
        test_check_python_missing_import,
        test_check_python_import_from_app_folder,
        test_execute_script_can_import_app_folder_module,
        test_check_python_import_top_name_only_and_try_excluded,
        test_check_python_bom_is_warn,
        test_check_python_cp932_is_error,
        test_check_python_dangerous_calls,
        test_check_python_no_python_exe,
        test_check_powershell_unavailable_is_unknown,
        test_check_powershell_cases,
        test_ps_allowlist_matching,
        test_truncate_lines_and_boundary,
        test_truncate_chars_and_boundary,
        test_build_result_text,
        test_build_result_text_with_command_id,
        test_summary_short_output_is_not_summarized,
        test_summary_long_output,
        test_summary_chars_trigger,
        test_summary_repeated_lines_collapsed,
        test_summary_mode_off_keeps_truncation,
        test_save_full_output,
        test_save_full_output_failure_falls_back,
        test_normalize_config_summary_defaults_and_clamps,
        test_parse_command_id,
        test_extract_summary,
        test_build_return_text,
        test_build_check_text,
        test_detect_policy_stop,
        test_build_loop_record_markdown_empty,
        test_build_loop_record_markdown_two_hands,
        test_build_loop_record_markdown_long_output,
        test_build_loop_record_markdown_no_command_id_and_timeout,
        test_save_full_output_if_long,
        test_build_result_text_reuses_full_output_path,
        test_result_ai_prompt_appended,
        test_diff_output_lines,
        test_build_result_diff_added_only,
        test_build_result_diff_removed_only,
        test_build_result_diff_identical,
        test_build_result_diff_no_previous,
        test_build_result_diff_trailing_newline,
        test_build_result_diff_caps_lines_and_notes_truncation,
        test_build_rerun_result_text,
        test_last_results_roundtrip_limit_and_truncation,
        test_execute_ok_and_nonzero_and_stderr_only,
        test_execute_japanese_output,
        test_execute_timeout_kills_tree,
        test_execute_stop_button,
        test_execute_missing_python,
        test_append_run_log,
        test_check_script_dispatch,
        test_list_candidates_excludes_bom_copies_and_stable_order,
        test_check_python_danger_respects_import_aliases,
        test_check_python_same_folder_module_found,
        test_check_python_cp932_coding_declaration,
        test_truncate_trailing_newline_and_crlf,
        test_build_check_preview_unknown,
        test_ps_member_warns_matching,
        test_ps1_bom_copy_source_cases,
        test_check_powershell_bomless_utf8_uses_copy,
        test_check_powershell_member_warns_via_stub,
        test_execute_powershell_bomless_utf8_uses_copy_and_cleans_up,
        test_execute_powershell_bom_copy_failure_is_encoding_error,
        test_execute_expected_sha256,
        test_execute_timeout_keeps_partial_output_and_closes_job,
        test_check_python_builtin_modules_found,
        test_run_handle_stop_closes_job,
        test_execute_stop_closes_job_once,
        test_cleanup_bom_copies,
        test_launch_restart_dispatch,
        test_respawn_when_gone_spawns_once_after_exit,
        test_respawn_when_gone_already_gone_spawns_immediately,
        test_respawn_when_gone_gives_up_on_timeout,
        test_phal_engine_check_and_execute,
        test_diag_events_roundtrip_and_corrupt,
        test_diag_bundle_composition_and_trim,
        test_diag_auto_state_roundtrip_and_corrupt,
        test_reset_auto_state_roundtrip_and_corrupt,
        test_rerun_auto_state_roundtrip_and_corrupt,
        test_auto_run_in_scope,
        test_stage_color_ratio_and_color,
        test_parse_progress_line,
        test_execute_streaming_output,
        test_phal_status_build_and_write_only_on_change,
        test_collect_intro_environment_with_fake_runner,
        test_collect_intro_environment_runner_missing_is_unknown,
        test_collect_intro_modules_truncated,
        test_build_intro_prompt,
        test_build_intro_prompt_unknowns,
        test_build_intro_prompt_mentions_phal_lib,
        test_build_ack_text,
        test_build_blocked_text,
        test_blocked_notified_roundtrip_and_corrupt,
        test_extract_attach_lines,
        test_extract_attach_lines_rejects_paths,
        test_attach_file_paths_and_outbox,
        test_zip_as_txt_paths,
        test_settings_report_and_marker_auto_state,
        test_resolve_file_request_scope,
        test_find_full_output,
        test_build_history_text,
        test_log_request_parse_select_reply,
        test_log_events_roundtrip_and_cap,
        test_log_filenames_do_not_collide,
        test_stop_auto_state,
        test_split_command_args,
        test_execute_script_with_args,
        test_cli_tools_sanitize_and_resolve,
        test_cli_tool_registry,
        test_execute_command_and_result_text,
        test_tool_auto_state_roundtrip,
        test_parse_file_request_specs,
        test_file_range_slicing,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
