# tests/test_diagnostics.py
# diagnostics.py の純関数を、合成した入力で確かめる。実物のログやイベントログには頼らない。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\test_diagnostics.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_bg_remove.py と同じ形。pytest があればそのまま拾える)。
# QR を QImage に描くところだけ Qt を使うので offscreen にしておく(窓は出さない)。
import os
import struct
import sys
import tempfile
import zlib
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import diagnostics as dg  # noqa: E402


# ---------------------------------------------------------------
# 伏せ字
# ---------------------------------------------------------------
HOME = r"C:\Users\Taro"
NAMES = ["Taro", "DESK-01X", "CORP"]


def test_redact_home_path():
    text = r'File "C:\Users\Taro\.venvs\tray-tools\Lib\x.py" and C:/Users/Taro/Documents/a.txt'
    out = dg.redact(text, home=HOME, names=NAMES)
    assert r"~\.venvs\tray-tools\Lib\x.py" in out, out
    assert "~/Documents/a.txt" in out, out
    assert "Taro" not in out and "Users" not in out


def test_redact_home_case_insensitive():
    out = dg.redact(r"c:\users\taro\AppData\x.log", home=HOME, names=NAMES)
    assert out.startswith("~"), out
    assert "taro" not in out.lower()


def test_redact_other_paths_to_basename():
    out = dg.redact(r"open D:\work\secret project\plan.xlsx failed; see E:\logs\a.log",
                    home=HOME, names=NAMES)
    # 空白を含むパスは引用符が無いと端が分からない。少なくともフォルダ名の先頭は残らない。
    assert r"D:\work" not in out and r"E:\logs" not in out, out
    assert "a.log" in out
    quoted = dg.redact("No such file: 'D:\\Program Files\\App\\conf.ini'", home=HOME, names=NAMES)
    assert quoted == "No such file: 'conf.ini'", quoted
    unc = dg.redact(r"\\fileserver\share\dept\book.xlsx", home=HOME, names=NAMES)
    assert unc == "book.xlsx", unc


def test_redact_names_case_insensitive():
    text = "user TARO on desk-01x domain corp / Taro's PC DESK-01X"
    out = dg.redact(text, home=HOME, names=NAMES)
    assert "taro" not in out.lower() and "desk-01x" not in out.lower() and "corp" not in out.lower(), out
    assert out.count("*") == 5, out


def test_redact_short_name_only_as_word():
    out = dg.redact("ab cab abc ab.", home=HOME, names=["ab"])
    assert out == "* cab abc *.", out


def test_redact_email():
    out = dg.redact("mail taro.yamada+x@example.co.jp now", home=HOME, names=NAMES)
    assert "@" not in out and "<email>" in out, out


def test_redact_ip_addresses():
    out = dg.redact("connect 192.168.1.20:8080 and 10.0.0.1, v6 fe80::1ff:fe23:4567:890a", home=HOME, names=[])
    assert "192.168" not in out and "10.0.0.1" not in out and "fe80" not in out, out
    assert out.count("<ip>") == 3, out
    # 時刻や Windows のビルド番号は IP と取り違えない。
    kept = dg.redact("10-15 21:39:51 win 10.0.26200.9711", home=HOME, names=[])
    assert kept == "10-15 21:39:51 win 10.0.26200.9711", kept


def test_redact_url():
    out = dg.redact("GET https://api.example.com/x?token=abc failed", home=HOME, names=[])
    assert "token" not in out and "<url>" in out, out


# ---------------------------------------------------------------
# crash.log
# ---------------------------------------------------------------
CRASH = "\r\r\n".join([
    "",
    "===== 2026-09-01 10:00:00 起動 =====",
    "",
    "===== 2026-09-02 11:00:00 起動 =====",
    "Windows fatal exception: access violation",
    "",
    "Thread 0x00001354 [Thread-2 (process)] (most recent call first):",
    '  File "C:\\Python\\Lib\\threading.py", line 369 in wait',
    '  File "C:\\Python\\Lib\\queue.py", line 199 in get',
    "",
    "Current thread 0x00003a70 [agent-loop-menu] (most recent call first):",
    "  Garbage-collecting",
    '  File "C:\\venv\\comtypes\\unknwn.py", line 420 in Release',
    '  File "C:\\venv\\comtypes\\unknwn.py", line 288 in __del__',
    '  File "R:\\repo\\feature_screen.py", line 12 in build',
    "",
    "Current thread's C stack trace (most recent call first):",
    "  <cannot get C stack on this system>",
    "",
    "===== 2026-09-03 12:00:00 起動 =====",
    "Windows fatal exception: code 0x8001010d",
    "",
    "Current thread 0x00004a1c (most recent call first):",
    '  File "R:\\repo\\main.py", line 971 in main',
    '  File "R:\\repo\\main.py", line 975 in <module>',
    "",
    "===== 2026-09-04 13:00:00 起動 =====",
    "",
])


def test_crash_log_starts_and_frames():
    info = dg.parse_crash_log(CRASH, count=6)
    assert len(info["starts"]) == 4
    assert info["fatal_total"] == 2
    first, second = info["fatals"]
    # 起動見出しとの対応
    assert first["start"] == datetime(2026, 9, 2, 11, 0, 0)
    assert second["start"] == datetime(2026, 9, 3, 12, 0, 0)
    # 落ちたスレッド(Current thread)の上位3フレーム。パスはファイル名だけ。
    assert first["kind"] == "AV"
    assert first["thread"] == "agent-loop-menu"
    assert first["frames"] == [("GC", "", ""), ("unknwn.py", "420", "Release"),
                               ("unknwn.py", "288", "__del__")], first["frames"]
    assert first["trigger"] == [("feature_screen.py", "12", "build")], first["trigger"]
    assert not first["maybe_handled"]
    # 処理された例外かもしれないものには印
    assert second["kind"] == "0x8001010d" and second["maybe_handled"]
    assert second["frames"][0] == ("main.py", "971", "main")


def test_crash_log_count_and_summary():
    info = dg.parse_crash_log(CRASH, count=1)
    assert len(info["fatals"]) == 1 and info["fatals"][0]["kind"] == "0x8001010d"
    lines = dg.summarize_crash(dg.parse_crash_log(CRASH, count=6))
    assert lines[0] == "crash fatal 2 (last 2)"
    assert lines[1].startswith(" 09-03 12:00 0x8001010d? main.py:971 main"), lines
    # GC → Release の形は、GC を呼び込んだ側のフレームを出す
    assert "[agent-loop-menu] GC>Release @ feature_screen.py:12 build" in lines[2], lines
    assert lines[-1].startswith(" ?=")


def test_crash_log_groups_repeats():
    block = "\n".join([
        "===== 2026-09-05 0{}:00:00 起動 =====",
        "Windows fatal exception: access violation",
        "Current thread 0x1 (most recent call first):",
        '  File "C:\\x\\a.py", line 1 in f',
    ])
    text = "\n".join(block.format(i) for i in range(1, 4))
    lines = dg.summarize_crash(dg.parse_crash_log(text, count=6))
    assert lines[1] == " 09-05 03:00 x3 AV a.py:1 f", lines
    assert len(lines) == 2


def test_starts_summary():
    starts = [datetime(2026, 10, 1), datetime(2026, 10, 18, 9, 5), datetime(2026, 10, 20, 8, 0)]
    now = datetime(2026, 10, 21, 12, 0)
    text = dg.summarize_starts(starts, now, process_start=now - timedelta(days=1, hours=2, minutes=3))
    assert text == "boot 7d 2 last 10-20 08:00 up 1d02h03m", text


# ---------------------------------------------------------------
# error.log
# ---------------------------------------------------------------
ERRORS = "\n".join([
    "",
    "===== 2026-08-24 23:51:24 uncaught =====",
    "TypeError: f() missing 1 required positional argument: 'checked'",
    "",
    "===== 2026-09-11 01:00:27 command=agent-loop args=['start'] =====",
    "Traceback (most recent call last):",
    '  File "R:\\repo\\main.py", line 311, in _handle_connection',
    "    handler(args, reply)",
    "NameError: name 'screen' is not defined",
    "",
    "===== 2026-09-12 10:00:00 rapture open =====",
    "Traceback (most recent call last):",
    '  File "x.py", line 1, in g',
    "OSError: [WinError 5] denied",
    "",
    "During handling of the above exception, another exception occurred:",
    "",
    "Traceback (most recent call last):",
    '  File "x.py", line 2, in g',
    "_ctypes.COMError: (-2147467259, 'fail', (None,))",
    "second line of message",
    "",
])


def test_error_log_entries():
    info = dg.parse_error_log(ERRORS, count=6)
    assert info["total"] == 3
    a, b, c = info["entries"]
    assert (a["type"], a["where"]) == ("TypeError", "uncaught")
    assert a["message"].startswith("f() missing")
    assert (b["type"], b["message"], b["where"]) == ("NameError", "name 'screen' is not defined", "command=agent-loop")
    # 連鎖した例外は最後のもの。メッセージは1行目だけ。
    assert c["type"] == "_ctypes.COMError" and c["message"].startswith("(-2147467259"), c
    assert c["time"] == datetime(2026, 9, 12, 10, 0, 0)
    lines = dg.summarize_errors(dg.parse_error_log(ERRORS, count=2))
    assert lines[0] == "err 3 (last 2)"
    assert lines[1].startswith(" 09-12 10:00 rapture _ctypes.COMError: ("), lines


# ---------------------------------------------------------------
# イベントログの XML
# ---------------------------------------------------------------
def _event(event_id, utc, data_xml, message=""):
    rendering = f"<RenderingInfo Culture='ja-JP'><Message>{message}</Message></RenderingInfo>" if message else ""
    return (f"<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
            f"<Provider Name='Application Error'/><EventID>{event_id}</EventID>"
            f"<TimeCreated SystemTime='{utc}'/><Computer>DESK</Computer></System>"
            f"<EventData>{data_xml}</EventData>{rendering}</Event>")


def _local(utc_text):
    return dg._utc_text_to_local(utc_text)


def test_event_xml_named_fields():
    xml = _event(1000, "2026-10-17T04:05:47.3654820Z",
                 "<Data Name='AppName'>pythonw.exe</Data><Data Name='ModuleName'>_ctypes.pyd</Data>"
                 "<Data Name='ExceptionCode'>c0000005</Data><Data Name='ProcessCreationTime'>0x1dd5dec31ad9eb5</Data>"
                 "<Data Name='AppPath'>C:\\Py\\pythonw.exe</Data><Data Name='ModulePath'>C:\\Py\\DLLs\\_ctypes.pyd</Data>")
    other = _event(1000, "2026-10-17T05:00:00.0Z",
                   "<Data Name='AppName'>notepad.exe</Data><Data Name='ModuleName'>x.dll</Data>")
    events = dg.parse_event_xml(other + "\r\n" + xml)
    assert len(events) == 1  # python を含むものだけ
    e = events[0]
    assert (e["app"], e["module"], e["code"], e["hang"]) == ("pythonw.exe", "_ctypes.pyd", "c0000005", False)
    assert e["time"] == _local("2026-10-17T04:05:47")
    assert e["created"] is not None and abs((e["time"] - e["created"]).total_seconds() - 256) < 2


def test_event_xml_english_message():
    message = ("Faulting application name: python.exe, version: 3.14.4150.1013, time stamp: 0x69d511dd&#13;&#10;"
               "Faulting module name: Qt6Core.dll, version: 6.11.1.0, time stamp: 0x69fcb7c0&#13;&#10;"
               "Exception code: 0xc0000409&#13;&#10;"
               "Faulting application path: C:\\Py\\python.exe&#13;&#10;"
               "Faulting module path: C:\\Users\\a\\.venvs\\tray-tools\\Lib\\PySide6\\Qt6Core.dll&#13;&#10;")
    events = dg.parse_event_xml(_event(1000, "2026-09-10T15:42:53Z", "", message))
    e = events[0]
    assert (e["app"], e["module"], e["code"]) == ("python.exe", "Qt6Core.dll", "c0000409"), e
    assert any("tray-tools" in p for p in e["paths"]), e["paths"]


def test_event_xml_japanese_message():
    message = ("障害が発生しているアプリケーション名: pythonw.exe、バージョン: 3.14.4150.1013、タイム スタンプ: 0x69d511de&#13;&#10;"
               "障害が発生したモジュール名: _winrt_dom.pyd、 バージョン: 0.0.0.0、タイム スタンプ: 0x68ce31fa&#13;&#10;"
               "例外コード: 0xc0000005&#13;&#10;"
               "Faulting アプリケーション パス:  C:\\Py\\pythonw.exe&#13;&#10;"
               "Faulting モジュール パス:  C:\\lan-drop\\.venv\\_winrt_dom.pyd&#13;&#10;")
    e = dg.parse_event_xml(_event(1000, "2026-10-17T04:05:47Z", "", message))[0]
    assert (e["app"], e["module"], e["code"]) == ("pythonw.exe", "_winrt_dom.pyd", "c0000005"), e
    assert any(p.endswith("_winrt_dom.pyd") for p in e["paths"]), e["paths"]
    hang = dg.parse_event_xml(_event(
        1002, "2026-09-12T02:12:44Z", "",
        "プログラム pythonw.exe バージョン 3.14.4150.1013 は Windows との対話を停止し、閉じられました。"))[0]
    assert hang["hang"] and hang["app"] == "pythonw.exe"


def test_event_xml_positional_hang():
    data = ("<Data>pythonw.exe</Data><Data>3.14</Data><Data>0x7340</Data><Data>0x1dd4259357313a1</Data>"
            "<Data>4294967295</Data><Data>C:\\Users\\a\\.venvs\\tray-tools\\Scripts\\pythonw.exe</Data>")
    e = dg.parse_event_xml(_event(1002, "2026-09-12T02:12:44Z", data))[0]
    assert e["hang"] and e["app"] == "pythonw.exe" and e["created"] is not None
    assert e["paths"][-1].endswith("pythonw.exe")


def test_event_classify_and_summary():
    start = datetime(2026, 10, 1, 12, 0, 10)
    tray_by_path = {"time": datetime(2026, 10, 2, 9, 0), "app": "python.exe", "module": "Qt6Core.dll",
                    "code": "c0000409", "paths": [r"C:\Users\a\.venvs\tray-tools\Lib\Qt6Core.dll"],
                    "created": None, "hang": False}
    tray_by_start = {"time": datetime(2026, 10, 1, 13, 0), "app": "pythonw.exe", "module": "_ctypes.pyd",
                     "code": "c0000005", "paths": [r"C:\Py\DLLs\_ctypes.pyd"],
                     "created": datetime(2026, 10, 1, 12, 0, 5), "hang": False}
    other = {"time": datetime(2026, 10, 3, 1, 0), "app": "pythonw.exe", "module": "_winrt.pyd",
             "code": "c0000005", "paths": [r"C:\lan-drop\.venv\_winrt.pyd"],
             "created": datetime(2026, 10, 3, 0, 0), "hang": False}
    # 見出しは秒までなので、作成時刻(ミリ秒まで)より少し早く見えることがある。
    same_second = dict(tray_by_start, created=datetime(2026, 10, 4, 8, 0, 0, 159000))
    tray, others = dg.classify_events([other, tray_by_path, tray_by_start, same_second], ["tray-tools"],
                                      [start, datetime(2026, 10, 4, 8, 0, 0)])
    assert tray == [tray_by_path, tray_by_start, same_second] and others == [other]
    tray = tray[:2]
    lines = dg.summarize_events("ok", tray, others, count=6)
    assert lines[0] == "evt 30d tray 2 other 1"
    assert lines[1] == " 10-02 09:00 python Qt6Core.dll c0000409"
    assert dg.summarize_events("denied", [], [], 6) == ["evt 30d 読めず (denied)"]
    assert dg.summarize_events("error", [], [], 6)[0].startswith("evt 30d 読めず")


# ---------------------------------------------------------------
# .git の読み取り
# ---------------------------------------------------------------
COMMIT_BODY = (b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
               b"author A <a@example.com> 1760000000 +0900\n"
               b"committer A <a@example.com> 1760000000 +0900\n\nmsg\n")


def _commit_sha():
    store = b"commit %d\0" % len(COMMIT_BODY) + COMMIT_BODY
    return hashlib.sha1(store).hexdigest(), store


def test_git_loose_ref_and_object():
    sha, store = _commit_sha()
    with tempfile.TemporaryDirectory() as tmp:
        git = Path(tmp) / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs" / "heads" / "main").write_text(sha + "\n")
        obj = git / "objects" / sha[:2] / sha[2:]
        obj.parent.mkdir(parents=True)
        obj.write_bytes(zlib.compress(store))
        info = dg.read_git_version(Path(tmp))
    assert info["state"] == "git" and info["sha"] == sha and info["branch"] == "main"
    assert info["date"] == datetime.fromtimestamp(1760000000)
    assert dg.summarize_version(info).startswith(f"ver {sha[:7]} ")


def _write_pack(git: Path, sha: str, body: bytes):
    """コミット1つだけの pack と idx(v2)を作る。"""
    pack_dir = git / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    size = len(body)
    header = bytearray()
    byte = (1 << 4) | (size & 0x0F)
    size >>= 4
    while size:
        header.append(byte | 0x80)
        byte = size & 0x7F
        size >>= 7
    header.append(byte)
    pack = b"PACK" + struct.pack(">II", 2, 1)
    offset = len(pack)
    pack += bytes(header) + zlib.compress(body)
    pack += hashlib.sha1(pack).digest()
    (pack_dir / "pack-test.pack").write_bytes(pack)
    raw = bytes.fromhex(sha)
    fanout = [0] * 256
    for i in range(raw[0], 256):
        fanout[i] = 1
    idx = b"\xfftOc" + struct.pack(">I", 2) + struct.pack(">256I", *fanout)
    idx += raw + struct.pack(">I", 0) + struct.pack(">I", offset)
    (pack_dir / "pack-test.idx").write_bytes(idx)


def test_git_packed_refs_and_pack():
    sha, _ = _commit_sha()
    with tempfile.TemporaryDirectory() as tmp:
        git = Path(tmp) / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/dev\n")
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{'1' * 40} refs/heads/main\n{sha} refs/heads/dev\n^{'2' * 40}\n")
        _write_pack(git, sha, COMMIT_BODY)
        info = dg.read_git_version(Path(tmp))
    assert info["state"] == "git" and info["sha"] == sha and info["branch"] == "dev", info
    assert info["date"] == datetime.fromtimestamp(1760000000)


def test_git_missing_and_broken():
    with tempfile.TemporaryDirectory() as tmp:
        assert dg.read_git_version(Path(tmp)) == {"state": "nogit"}
        assert dg.summarize_version({"state": "nogit"}) == "ver git なし"
        git = Path(tmp) / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/gone\n")
        info = dg.read_git_version(Path(tmp))
    assert info["state"] == "broken"
    # 日付が読めなくても版の ID は出す
    assert dg.summarize_version({"state": "git", "sha": "a" * 40, "branch": "main", "date": None}) == \
        "ver aaaaaaa date? main"


# ---------------------------------------------------------------
# import の抜け
# ---------------------------------------------------------------
def test_scan_files_missing_imports():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tools").mkdir()
        (root / "main.py").write_text(
            "import os\nimport helper\nimport gone_module\nfrom pkg import x\n"
            "try:\n    import optional_dep\nexcept ImportError:\n    pass\n"
            "def f():\n    import lazy_gone\n", encoding="utf-8")
        (root / "helper.py").write_text("import third_party\n", encoding="utf-8")
        (root / "pkg").mkdir()
        (root / "tools" / "probe.py").write_text("import helper\nimport tool_gone\n", encoding="utf-8")
        (root / "broken.py").write_text("def x(:\n", encoding="utf-8")
        info = dg.scan_files(root, is_installed=lambda name: name == "third_party")
    names = [n for n, _ in info["missing"]]
    assert names == ["gone_module", "lazy_gone", "tool_gone"], info["missing"]
    assert dict(info["missing"])["tool_gone"] == "probe.py"
    assert info["broken"] == ["broken.py"]
    assert info["count"] == 4
    lines = dg.summarize_files(info)
    assert lines[1].startswith("lost gone_module(main.py)"), lines
    assert lines[2] == "broken broken.py"


def test_scan_repo_has_no_missing():
    info = dg.scan_files(dg.REPO_DIR)
    assert info["missing"] == [], info["missing"]
    assert info["broken"] == []


# ---------------------------------------------------------------
# 設定
# ---------------------------------------------------------------
def test_settings_summary_only_counts():
    settings = {"hotkeys": {"a": "ctrl+alt+a", "b": "", "c": "win+j"},
                "taskbar_widget": {"enabled": True},
                "launcher": {"bookmarks": [{"name": "秘密のフォルダ", "path": r"D:\secret"}]},
                "audio": {"devices": [{"label": "x"}, {"label": "y"}]}}
    lines = dg.summarize_settings(settings, {"jiggler": True, "keep_awake": False, "sticky": 2})
    assert lines[0] == "cfg hk 2/3 tbw on prewarm on audio 2 bm 1 pushcfg off", lines
    assert lines[1] == "run jig on awake off sticky 2", lines
    assert "secret" not in " ".join(lines) and "秘密" not in " ".join(lines)


# ---------------------------------------------------------------
# ページ分け
# ---------------------------------------------------------------
def _lines(n, width=40):
    return "\n".join(f"line{i:03d} " + "x" * (width - 8) for i in range(n))


def test_split_single_page_at_limit():
    limit = 200
    text = "a" * 200
    assert dg.split_pages(text, limit) == [text]
    assert len(dg.split_pages("a" * 201, limit)) == 2


def test_split_pages_marks_and_limits():
    text = _lines(60)
    limit = 400
    pages = dg.split_pages(text, limit)
    assert len(pages) > 1
    total = len(pages)
    original = text.split("\n")
    rebuilt = []
    for i, page in enumerate(pages, 1):
        assert len(page.encode("utf-8")) <= limit, (i, len(page.encode("utf-8")))
        head, _, body = page.partition("\n")
        assert head == f"[tray-tools diag {i}/{total}]"
        for line in body.split("\n"):
            assert line in original, line  # 行の途中で切れていない
        rebuilt.extend(body.split("\n"))
    assert rebuilt == original


def test_split_pages_utf8_bytes_and_many_pages():
    text = "\n".join("日本語の行 " + str(i) for i in range(400))
    limit = 120
    pages = dg.split_pages(text, limit)
    assert len(pages) >= 10
    for page in pages:
        assert len(page.encode("utf-8")) <= limit
    assert pages[0].startswith(f"[tray-tools diag 1/{len(pages)}]")
    assert pages[-1].startswith(f"[tray-tools diag {len(pages)}/{len(pages)}]")


# ---------------------------------------------------------------
# QR
# ---------------------------------------------------------------
def test_qr_version_fits_limit():
    page = ("a" * 79 + "\n") * (dg.PAGE_LIMIT_BYTES // 80)
    page = page[:dg.PAGE_LIMIT_BYTES]
    assert len(page.encode("utf-8")) == dg.PAGE_LIMIT_BYTES
    qr = dg.make_qr(page)
    assert not qr.is_micro
    assert qr.version <= dg.QR_MAX_VERSION, qr.version
    assert qr.error.lower() == dg.QR_ERROR
    matrix = list(qr.matrix)
    assert len(matrix) == 17 + 4 * qr.version
    assert all(len(row) == len(matrix) for row in matrix)
    # 上限いっぱいのページ(日本語混じり)も version 25 に収まる
    jp = dg.split_pages("\n".join("日本語 " * 8 for _ in range(200)))
    for p in jp:
        assert dg.make_qr(p).version <= dg.QR_MAX_VERSION


def test_qr_to_qimage():
    from PySide6.QtGui import QColor, QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    qr = dg.make_qr("[tray-tools diag] hello")
    cells = len(list(qr.matrix))
    image = dg.qr_to_qimage(qr, scale=3)
    assert image.width() == image.height() == (cells + 2 * dg.QR_BORDER) * 3
    # quiet zone は白、左上の位置検出パターンの角は黒
    assert QColor(image.pixel(0, 0)).name() == "#ffffff"
    corner = dg.QR_BORDER * 3
    assert QColor(image.pixel(corner, corner)).name() == "#000000"
    assert QColor(image.pixel(corner - 1, corner)).name() == "#ffffff"


# ---------------------------------------------------------------
# 窓の配色(bg_remove.THEME と同じ値で、読める組み合わせになっていること)
# ---------------------------------------------------------------
def test_window_theme_matches_bg_remove_and_contrast():
    import bg_remove
    import diagnostics_window as dw
    for key, value in dw.THEME.items():
        assert bg_remove.THEME[key] == value, key
    for fg, bg, need in dw.CONTRAST_REQUIREMENTS:
        ratio = bg_remove.contrast_ratio(dw.THEME[fg], dw.THEME[bg])
        assert ratio >= need, (fg, bg, ratio)


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
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
