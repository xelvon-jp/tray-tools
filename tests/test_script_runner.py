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
    broken = core.normalize_config({
        "recent_hours": "x", "max_items": 0, "timeout_seconds": 99999,
        "window_geometry": [1, 2], "python": "", "always_on_top": 1})
    assert broken["recent_hours"] == 24
    assert broken["max_items"] == 1
    assert broken["timeout_seconds"] == 3600
    assert broken["window_geometry"] is None
    assert broken["python"] == "python"
    assert broken["always_on_top"] is True


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
        test_build_check_text,
        test_detect_policy_stop,
        test_execute_ok_and_nonzero_and_stderr_only,
        test_execute_japanese_output,
        test_execute_timeout_kills_tree,
        test_execute_stop_button,
        test_execute_missing_python,
        test_append_run_log,
        test_check_script_dispatch,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
