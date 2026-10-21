# tests/test_com_gc.py
# 「GC → comtypes の __del__ → Release で access violation」(crash.log の持病)の再現テスト。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\test_com_gc.py
#
# 落ちるかどうかは子プロセスで確かめる(落ちる側を自分のプロセスで走らせると、テストごと死ぬ)。
# 子は tools/com_gc_harness.py。常駐と同じくメイン＋別スレッド2本で COM の読み取りを回し、
# 合間に小さな循環を作って GC を誘発する。**読み取りしかしない**(音量・既定デバイス・
# ミュートは変えない。キー送信・マウス操作・窓の前面化もしない)。
#
# 実機の音声デバイス(マイク)を使う。マイクが無いPCでは再現側のテストを飛ばす。
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_diagnostics.py と同じ形)。
import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tools" / "com_gc_harness.py"
sys.path.insert(0, str(ROOT))

ACCESS_VIOLATION = 0xC0000005
_AV_TEXT = re.compile(r"access violation|without VTable|Garbage-collecting", re.I)


class Skip(Exception):
    pass


def _run_harness(*args, seconds=6, env_extra=None):
    env = dict(os.environ)
    env.pop("TRAYTOOLS_COM_GC_PROBE", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", str(HARNESS), "--seconds", str(seconds), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _crashed(code, out) -> bool:
    return (code & 0xFFFFFFFF) == ACCESS_VIOLATION or code < 0 or bool(_AV_TEXT.search(out))


def _has_microphone() -> bool:
    import mic_control

    return mic_control.get_mute() is not None


# ---------------------------------------------------------------
# 直す前の書き方は落ちる(再現できていることの確認)
# ---------------------------------------------------------------
def test_legacy_cast_pattern_crashes():
    """ctypes.cast で型を変えていた旧 mic_control は、循環 GC の Release で落ちる。

    確率で落ちるので、3回まで試して1回でも落ちれば再現とみなす(実測は 5/5 で落ちた)。"""
    if not _has_microphone():
        raise Skip("マイクが無い")
    results = []
    for _ in range(3):
        code, out = _run_harness("--only", "mic_legacy", "--threads", "2", seconds=6)
        results.append(code)
        if _crashed(code, out):
            assert "Release" in out or (code & 0xFFFFFFFF) == ACCESS_VIOLATION or "access violation" in out, out[-2000:]
            return
    raise AssertionError(f"旧い書き方で落ちなかった(再現できていない) exit={results}")


def test_probe_names_the_legacy_cast_as_culprit():
    """調査モードは旧い書き方を犯人として記録し、しかも落ちない。"""
    if not _has_microphone():
        raise Skip("マイクが無い")
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "probe.log"
        code, out = _run_harness("--only", "mic_legacy", "--threads", "2", "--probe", seconds=4,
                                 env_extra={"TRAYTOOLS_COM_GC_PROBE_LOG": str(log)})
        assert code == 0, f"調査モードなのに落ちた exit={code}\n{out[-2000:]}"
        text = log.read_text(encoding="utf-8")
    m = re.search(r"'gc_finalized': (\d+)", out)
    assert m and int(m.group(1)) > 0, out[-1000:]
    assert "GARBAGE_COM type=POINTER(IUnknown)" in text, text[:2000]
    assert "_legacy_mic_get_mute" in text, text[:2000]
    assert "ctypes.cast の元にされた形" in text, text[:2000]


# ---------------------------------------------------------------
# 直したあとは落ちず、常駐の COM の経路から循環ゴミが出ない
# ---------------------------------------------------------------
def test_fixed_mic_control_does_not_crash():
    if not _has_microphone():
        raise Skip("マイクが無い")
    for _ in range(3):
        code, out = _run_harness("--only", "mic", "--threads", "2", seconds=6)
        assert code == 0 and not _crashed(code, out), f"exit={code}\n{out[-2000:]}"


def test_resident_com_paths_leave_no_com_garbage():
    """常駐が使う COM の読み取り経路(マイク・音量・既定デバイス・デバイス列挙・
    エクスプローラの窓列挙)を叩いて、循環 GC に回ってくる COM ポインタが1つも無いこと。

    1つでも出たら、それが次の「GC → Release で即死」の種。com_gc_probe.log の
    GARBAGE_COM の行に、作った場所と抱えていたものが出る。"""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "probe.log"
        code, out = _run_harness("--only", "mic,audio,list,explorer", "--threads", "2", "--probe",
                                 seconds=8, env_extra={"TRAYTOOLS_COM_GC_PROBE_LOG": str(log)})
        text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert code == 0, f"exit={code}\n{out[-2000:]}"
    m = re.search(r"'created': (\d+), 'gc_finalized': (\d+), 'cross_thread_release': (\d+)", out)
    assert m, out[-1000:]
    created, finalized, cross = map(int, m.groups())
    assert created > 0, "COM ポインタが1つも作られていない(経路を叩けていない)"
    assert finalized == 0, f"循環 GC に COM ポインタが回ってきた: {finalized}\n{text[:3000]}"
    assert "GARBAGE_COM" not in text and "RELEASE_FAILED" not in text, text[:3000]


def test_mic_get_mute_returns_bool_or_none():
    import mic_control

    value = mic_control.get_mute()
    assert value is None or isinstance(value, bool), value


# ---------------------------------------------------------------
# 書き方の見張り
# ---------------------------------------------------------------
def test_no_ctypes_cast_in_com_modules():
    """comtypes / pycaw を使うモジュールで ctypes.cast を使っていないこと。

    COM ポインタを cast すると AddRef されず、元のポインタが循環に入る(mic_control の
    _with_endpoint_volume の docstring)。型を変えるなら QueryInterface を使う。"""
    offenders = []
    for path in sorted(ROOT.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not re.search(r"^\s*(import|from)\s+(comtypes|pycaw)", text, re.M):
            continue
        # 文字列(docstring の説明)は数えない。実際の呼び出しだけを見る。
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "cast":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "\n".join(offenders)


def test_probe_is_off_by_default():
    import com_gc_probe

    with tempfile.TemporaryDirectory() as tmp:
        settings = Path(tmp) / "settings.json"
        old = os.environ.pop("TRAYTOOLS_COM_GC_PROBE", None)
        try:
            assert com_gc_probe.is_requested(settings) is False  # ファイルが無い
            settings.write_text('{"audio": {}}', encoding="utf-8")
            assert com_gc_probe.is_requested(settings) is False
            settings.write_text('{"debug": {"com_gc_probe": false}}', encoding="utf-8")
            assert com_gc_probe.is_requested(settings) is False
            settings.write_text('{"debug": {"com_gc_probe": true}}', encoding="utf-8")
            assert com_gc_probe.is_requested(settings) is True
            settings.write_text('{"audio": {}}', encoding="utf-8")
            os.environ["TRAYTOOLS_COM_GC_PROBE"] = "1"
            assert com_gc_probe.is_requested(settings) is True
        finally:
            os.environ.pop("TRAYTOOLS_COM_GC_PROBE", None)
            if old is not None:
                os.environ["TRAYTOOLS_COM_GC_PROBE"] = old
    assert com_gc_probe.enabled() is False
    assert com_gc_probe.scan() == 0
    assert com_gc_probe.start_timer() is None


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Skip as e:
            skipped += 1
            print(f"skip  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed - skipped}/{len(tests)} passed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
