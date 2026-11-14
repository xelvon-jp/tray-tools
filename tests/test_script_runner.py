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
    """診断の自動応答の控え (1-43): 保存と読み直し・壊れたファイル。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "diag_auto.json"
        # 無いファイルは「消費した物は無く、送ったことも無い」。
        assert core.load_diag_auto(path) == {
            "consumed_id": "", "last_sent_at": 0.0}
        core.save_diag_auto(
            {"consumed_id": "id-1", "last_sent_at": 123.5}, path)
        assert core.load_diag_auto(path) == {
            "consumed_id": "id-1", "last_sent_at": 123.5}
        # 壊れたファイル・形の違う中身は既定に倒す。
        path.write_text("{壊れている", encoding="utf-8")
        assert core.load_diag_auto(path) == {
            "consumed_id": "", "last_sent_at": 0.0}
        path.write_text(json.dumps(
            {"consumed_id": 42, "last_sent_at": "x"}),
            encoding="utf-8")
        assert core.load_diag_auto(path) == {
            "consumed_id": "", "last_sent_at": 0.0}
        # 間隔は5分。
        assert core.DIAG_AUTO_COOLDOWN_SEC == 300.0


if __name__ == "__main__":
    tests = [
        test_normalize_config_defaults_and_clamps,
        test_list_candidates_filters_and_orders,
        test_file_sha256_changes_with_content,
        test_check_python_ok,
        test_check_python_unclosed_paren,
        test_check_python_indent_error,
        test_check_python_missing_import,
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
        test_parse_command_id,
        test_extract_summary,
        test_build_result_text_with_command_id,
        test_build_return_text,
        test_build_check_text,
        test_detect_policy_stop,
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
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
