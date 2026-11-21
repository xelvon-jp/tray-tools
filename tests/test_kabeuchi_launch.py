# test_kabeuchi_launch.py
# 壁打ち文字起こし(kabeuchi_stt)を別プロセスで起こす入口(kabeuchi_launch.py)と、それに付随する
# ホットキー名の解決(hotkeys._combo_for)・設定の既定値・setup.py --with-whisper のテスト。
#
#   %USERPROFILE%\.venvs\tray-tools\Scripts\python.exe tests\test_kabeuchi_launch.py
#
# 窓は出さない・マイクは開かない・モデルは取得しない・pip は走らせない。
# 実際にプロセスを起こすテストは、引数を書き出すだけのダミーのコマンド(.cmd)で行う
# (kabeuchi_stt 本体は起こさない)。
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import kabeuchi_launch as kl  # noqa: E402


def _make_venv(base: Path, packages) -> Path:
    """中身だけ venv の形をした偽物を作り、pythonw.exe のパスを返す(実行はしない)。"""
    (base / "Scripts").mkdir(parents=True)
    (base / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    site = base / "Lib" / "site-packages"
    site.mkdir(parents=True)
    for name in packages:
        if name == "sounddevice":
            (site / "sounddevice.py").write_text("", encoding="utf-8")
        else:
            (site / name).mkdir()
    exe = base / "Scripts" / "pythonw.exe"
    exe.write_text("", encoding="utf-8")
    return exe


# ---------------------------------------------------------------
# 起動コマンド
# ---------------------------------------------------------------
def test_build_argv_has_no_tray_and_script():
    argv = kl.build_argv("C:\\py\\pythonw.exe")
    assert argv[0] == "C:\\py\\pythonw.exe"
    assert Path(argv[1]) == ROOT / "kabeuchi_stt" / "main.py"
    assert argv[2:] == ["--no-tray"], argv            # トレイアイコンを増やさない
    assert kl.build_argv("p", script="s.py") == ["p", "s.py", "--no-tray"]


def test_script_exists_in_bundle():
    assert kl.SCRIPT_PATH.exists(), kl.SCRIPT_PATH


def test_server_name_matches_kabeuchi_stt():
    """起動側の「出して」の宛先は、kabeuchi_stt の単一起動の名前と同じでなければ届かない。"""
    text = (ROOT / "kabeuchi_stt" / "single_instance.py").read_text(encoding="utf-8")
    assert f'SERVER_NAME = "{kl.SERVER_NAME}"' in text


def test_default_python_location():
    p = kl.default_python(home="C:\\Users\\someone")
    assert Path(p) == Path("C:\\Users\\someone") / ".venvs" / "kabeuchi-stt" / "Scripts" / "pythonw.exe"


# ---------------------------------------------------------------
# 使う Python の決め方
# ---------------------------------------------------------------
def test_resolve_python_prefers_configured_and_pythonw():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        other = base / "other"
        other.mkdir()
        (other / "python.exe").write_text("", encoding="utf-8")
        (other / "pythonw.exe").write_text("", encoding="utf-8")
        # 設定の Python が最優先。python.exe を指定されたら、同じ場所の pythonw.exe(黒い窓が出ない)へ
        got = kl.resolve_python({"kabeuchi": {"python": str(other / "python.exe")}}, home=base)
        assert Path(got) == other / "pythonw.exe", got
        # pythonw.exe が無ければ指定のまま
        (other / "pythonw.exe").unlink()
        got = kl.resolve_python({"kabeuchi": {"python": str(other / "python.exe")}}, home=base)
        assert Path(got) == other / "python.exe", got


def test_resolve_python_configured_missing_is_error_naming_the_setting():
    with tempfile.TemporaryDirectory() as td:
        try:
            kl.resolve_python({"kabeuchi": {"python": "Z:\\no\\such\\pythonw.exe"}},
                              home=td, which=lambda name: None)
        except kl.LaunchError as e:
            assert "kabeuchi.python" in str(e) and "Z:\\no\\such\\pythonw.exe" in str(e), str(e)
        else:
            raise AssertionError("見つからない設定の Python を黙って使った")


def test_resolve_python_configured_without_dependencies_is_error():
    with tempfile.TemporaryDirectory() as td:
        exe = _make_venv(Path(td) / "v", ["numpy"])
        try:
            kl.resolve_python({"kabeuchi": {"python": str(exe)}}, home=td)
        except kl.LaunchError as e:
            assert "faster_whisper" in str(e) and "sounddevice" in str(e), str(e)
        else:
            raise AssertionError("依存の無い Python をそのまま使った")


def test_resolve_python_uses_dedicated_venv():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        exe = _make_venv(home / ".venvs" / "kabeuchi-stt", kl.REQUIRED_MODULES)
        assert Path(kl.resolve_python({}, home=home)) == exe
        assert Path(kl.resolve_python(None, home=home)) == exe
        # 設定が空文字・壊れた値でも既定へ
        assert Path(kl.resolve_python({"kabeuchi": {"python": ""}}, home=home)) == exe
        assert Path(kl.resolve_python({"kabeuchi": {"python": 3}}, home=home)) == exe
        assert Path(kl.resolve_python({"kabeuchi": "x"}, home=home)) == exe


def test_resolve_python_falls_back_to_tray_venv_only_when_whisper_importable():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        tray = home / "tray" / "pythonw.exe"
        tray.parent.mkdir()
        tray.write_text("", encoding="utf-8")
        has = lambda name: object()                       # noqa: E731  何でも見つかる
        none = lambda name: None                          # noqa: E731
        assert Path(kl.resolve_python({}, home=home, find_spec=has, tray_python=str(tray))) == tray
        # 一方だけ無い(sounddevice 無し)なら使わない
        only_fw = lambda name: object() if name == "faster_whisper" else None   # noqa: E731
        try:
            kl.resolve_python({}, home=home, find_spec=only_fw, tray_python=str(tray))
        except kl.LaunchError:
            pass
        else:
            raise AssertionError("依存が揃っていない tray-tools の venv を使った")
        try:
            kl.resolve_python({}, home=home, find_spec=none, tray_python=str(tray))
        except kl.LaunchError:
            pass
        else:
            raise AssertionError("faster_whisper が無いのに tray-tools の venv を使った")
        # find_spec が例外を投げても落ちない(使えない扱い)
        def boom(name):
            raise ValueError("x")
        try:
            kl.resolve_python({}, home=home, find_spec=boom, tray_python=str(tray))
        except kl.LaunchError:
            pass
        else:
            raise AssertionError("find_spec の例外で使えると判断した")


def test_resolve_python_dedicated_venv_without_deps_falls_back_or_explains():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        _make_venv(home / ".venvs" / "kabeuchi-stt", ["numpy"])
        try:
            kl.resolve_python({}, home=home, find_spec=lambda n: None)
        except kl.LaunchError as e:
            msg = str(e)
            assert "faster_whisper" in msg and kl.SETUP_COMMAND in msg, msg
        else:
            raise AssertionError("依存の足りない専用 venv を使った")


def test_resolve_python_nothing_available_message_names_setup_command():
    with tempfile.TemporaryDirectory() as td:
        try:
            kl.resolve_python({}, home=td, find_spec=lambda n: None)
        except kl.LaunchError as e:
            assert "python setup.py --with-whisper" in str(e), str(e)
            assert isinstance(e, OSError)
        else:
            raise AssertionError("何も無いのに Python が決まった")


def test_missing_dependencies_ignores_non_venv_layouts():
    with tempfile.TemporaryDirectory() as td:
        plain = Path(td) / "bin" / "python.exe"
        plain.parent.mkdir()
        plain.write_text("", encoding="utf-8")
        assert kl.missing_dependencies(plain) == []       # 中身が分からないものは止めない
        exe = _make_venv(Path(td) / "v", ["faster_whisper", "numpy"])
        assert kl.missing_dependencies(exe) == ["sounddevice", "PySide6"]


# ---------------------------------------------------------------
# 起こす
# ---------------------------------------------------------------
def test_spawn_builds_detached_no_tray_command():
    calls = []

    def fake_popen(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(pid=4242)

    with tempfile.TemporaryDirectory() as td:
        exe = _make_venv(Path(td) / "v", kl.REQUIRED_MODULES)
        pid = kl.spawn({"kabeuchi": {"python": str(exe)}}, popen=fake_popen, home=td)
        assert pid == 4242
        argv, kw = calls[0]
        assert argv == [str(exe), str(kl.SCRIPT_PATH), "--no-tray"], argv
        assert kw["creationflags"] & 0x00000008            # DETACHED_PROCESS(常駐を再起動しても道連れにしない)
        assert kw["creationflags"] & 0x00000200            # CREATE_NEW_PROCESS_GROUP
        assert Path(kw["cwd"]) == kl.SCRIPT_PATH.parent


def test_spawn_errors_are_launcherror_with_japanese_message():
    def bad_popen(argv, **kw):
        raise FileNotFoundError("nope")

    with tempfile.TemporaryDirectory() as td:
        exe = _make_venv(Path(td) / "v", kl.REQUIRED_MODULES)
        try:
            kl.spawn({"kabeuchi": {"python": str(exe)}}, popen=bad_popen, home=td)
        except kl.LaunchError as e:
            assert "起動できませんでした" in str(e)
        else:
            raise AssertionError("起動の失敗が握りつぶされた")
        # スクリプトが無い(tray-tools の更新が不完全)
        try:
            kl.spawn({"kabeuchi": {"python": str(exe)}}, script=Path(td) / "no" / "main.py", home=td)
        except kl.LaunchError as e:
            assert "見つかりません" in str(e)
        else:
            raise AssertionError("スクリプトが無いのに起こそうとした")


def test_spawn_really_starts_a_detached_process_with_no_tray():
    """ダミーのコマンド(.cmd)で、実際にプロセスが起き、--no-tray が渡ることを確かめる。
    kabeuchi_stt 本体は起こさない(窓は出ない。DETACHED_PROCESS でコンソールも付かない)。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        out = base / "args.txt"
        dummy = base / "dummy.cmd"
        dummy.write_text('@echo %* > "%~dp0args.txt"\r\n', encoding="utf-8")
        script = base / "kabeuchi_stt" / "main.py"
        script.parent.mkdir()
        script.write_text("", encoding="utf-8")
        pid = kl.spawn({"kabeuchi": {"python": str(dummy)}}, script=script, home=td)
        assert isinstance(pid, int) and pid > 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not out.exists():
            time.sleep(0.1)
        assert out.exists(), "ダミーのコマンドが実行されなかった"
        time.sleep(0.2)
        text = out.read_text(encoding="utf-8", errors="replace")
        assert "--no-tray" in text and "main.py" in text, text


def test_start_sends_show_when_running_else_spawns():
    spawned = []
    assert kl.start({}, sender=lambda: True, spawner=lambda s: spawned.append(s)) == "sent"
    assert spawned == []                                   # 動いていれば起こさない(窓は1つ)
    assert kl.start({}, sender=lambda: False, spawner=lambda s: spawned.append(s)) == "spawned"
    assert len(spawned) == 1

    def raising():
        raise RuntimeError("pipe")
    assert kl.start({}, sender=raising, spawner=lambda s: spawned.append(s)) == "spawned"
    assert len(spawned) == 2                               # 判定できなければ起こす側へ

    def failing_spawner(s):
        raise kl.LaunchError("だめ")
    try:
        kl.start({}, sender=lambda: False, spawner=failing_spawner)
    except kl.LaunchError:
        pass
    else:
        raise AssertionError("起動の失敗が呼ぶ側に届かない")


def test_module_does_not_import_heavy_packages():
    """常駐に重いものを乗せない(PySide6・faster-whisper・numpy を import しない)。"""
    code = (
        "import sys; sys.path.insert(0, %r); import kabeuchi_launch; "
        "bad = [m for m in ('PySide6', 'faster_whisper', 'numpy', 'sounddevice') if m in sys.modules]; "
        "sys.exit(1 if bad else 0)" % str(ROOT))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


# ---------------------------------------------------------------
# 設定・ホットキー
# ---------------------------------------------------------------
def test_settings_defaults_have_no_hotkey_assigned():
    import settings as settings_module

    section = settings_module.DEFAULT_SETTINGS["kabeuchi"]
    assert section == {"python": "", "hotkey": ""}, section   # 既定の割り当てはしない
    assert kl.configured_hotkey(settings_module.DEFAULT_SETTINGS) == ""
    assert "kabeuchi" not in settings_module.DEFAULT_SETTINGS["hotkeys"]
    assert kl.configured_hotkey({"kabeuchi": {"hotkey": " ctrl+alt+k "}}) == "ctrl+alt+k"
    assert kl.configured_hotkey({"kabeuchi": {"hotkey": 5}}) == ""
    assert kl.configured_hotkey({"kabeuchi": None}) == ""
    assert kl.configured_hotkey(None) == ""


def test_hotkey_dotted_name_reads_nested_setting():
    import hotkeys

    settings = {"hotkeys": {"a": "ctrl+alt+a"}, "kabeuchi": {"hotkey": "ctrl+alt+k"}}
    hk = settings["hotkeys"]
    assert hotkeys._combo_for(settings, hk, "a") == "ctrl+alt+a"           # 従来の名前は変わらない
    assert hotkeys._combo_for(settings, hk, "missing") is None
    assert hotkeys._combo_for(settings, hk, kl.HOTKEY_NAME) == "ctrl+alt+k"
    assert hotkeys._combo_for({"hotkeys": {}}, {}, kl.HOTKEY_NAME) is None   # 節が無い
    assert hotkeys._combo_for({"kabeuchi": {"hotkey": ""}}, {}, kl.HOTKEY_NAME) == ""
    assert hotkeys._combo_for({"kabeuchi": "x"}, {}, kl.HOTKEY_NAME) is None
    assert hotkeys._combo_for({"kabeuchi": {"hotkey": 7}}, {}, kl.HOTKEY_NAME) is None


def test_hotkey_registration_skips_empty_and_registers_set():
    """空(既定)なら keyboard に何も登録しない。設定されていれば、その組み合わせで登録する。"""
    import hotkeys

    added = []
    real = hotkeys.keyboard.add_hotkey
    hotkeys.keyboard.add_hotkey = lambda combo, cb: added.append(combo)
    try:
        handlers = {kl.HOTKEY_NAME: lambda: None}
        bridge = hotkeys.HotkeyBridge()
        assert hotkeys._register_hotkeys(bridge, {"hotkeys": {}, "kabeuchi": {"hotkey": ""}}, handlers) == []
        assert added == []
        got = hotkeys._register_hotkeys(bridge, {"hotkeys": {}, "kabeuchi": {"hotkey": "ctrl+alt+k"}}, handlers)
        assert got == [kl.HOTKEY_NAME] and added == ["ctrl+alt+k"], (got, added)
    finally:
        hotkeys.keyboard.add_hotkey = real


def test_screen_feature_wiring():
    """画面アイコンの機能が、ホットキーとメニューの入口を持つ。失敗は通知に受けて投げ切らない。"""
    try:
        import feature_screen
    except Exception as e:  # noqa: BLE001  依存の無い環境では確かめられない
        print(f"SKIP-INNER: feature_screen を読み込めません ({type(e).__name__})")
        return
    feat = object.__new__(feature_screen.ScreenFeature)
    handlers = feature_screen.ScreenFeature.hotkeys(feat)
    assert kl.HOTKEY_NAME in handlers
    notices = []
    feat._notify = lambda title, message: notices.append((title, message))
    feat.app_settings = {}
    real_start = kl.start
    try:
        kl.start = lambda settings: (_ for _ in ()).throw(kl.LaunchError("python setup.py --with-whisper を実行してください"))
        feat.start_kabeuchi()                              # 投げ切らない
        assert notices and "python setup.py --with-whisper" in notices[-1][1], notices
        kl.start = lambda settings: (_ for _ in ()).throw(RuntimeError("想定外"))
        feat.start_kabeuchi()
        assert "起動できませんでした" in notices[-1][1], notices
        n = len(notices)
        kl.start = lambda settings: "spawned"
        feat.start_kabeuchi()
        assert len(notices) == n                           # 成功時は通知しない
    finally:
        kl.start = real_start
    src = Path(feature_screen.__file__).read_text(encoding="utf-8")
    assert "🎙 壁打ち文字起こし…" in src


# ---------------------------------------------------------------
# setup.py --with-whisper(pip も venv 作成も走らせない)
# ---------------------------------------------------------------
class _FakeRun:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        text = " ".join(str(a) for a in argv)
        code = 1 if self.fail_on and self.fail_on in text else 0
        return SimpleNamespace(returncode=code)


def _run_install(with_whisper, fake_run, installed=False, tmp=None, requirements_exists=True):
    import setup as setup_module

    real_sub, real_py, real_req, real_dir = (
        setup_module.subprocess, setup_module.WHISPER_VENV_PYTHON, setup_module.WHISPER_REQUIREMENTS,
        setup_module.WHISPER_VENV_DIR)
    real_inst = setup_module._whisper_installed
    setup_module.subprocess = SimpleNamespace(run=fake_run)
    setup_module.WHISPER_VENV_DIR = tmp / "v"
    setup_module.WHISPER_VENV_PYTHON = tmp / "v" / "Scripts" / "python.exe"
    setup_module.WHISPER_REQUIREMENTS = tmp / "requirements-whisper.txt"
    if requirements_exists:
        setup_module.WHISPER_REQUIREMENTS.write_text("faster-whisper\n", encoding="utf-8")
    setup_module._whisper_installed = lambda: installed
    try:
        setup_module.install_whisper_optional(with_whisper)
    finally:
        (setup_module.subprocess, setup_module.WHISPER_VENV_PYTHON, setup_module.WHISPER_REQUIREMENTS,
         setup_module.WHISPER_VENV_DIR) = real_sub, real_py, real_req, real_dir
        setup_module._whisper_installed = real_inst


def test_setup_with_whisper_false_and_already_installed_do_nothing():
    with tempfile.TemporaryDirectory() as td:
        fake = _FakeRun()
        _run_install(False, fake, tmp=Path(td))
        assert fake.calls == []                            # --no-whisper: 何も走らない
        _run_install(True, fake, installed=True, tmp=Path(td))
        assert fake.calls == []                            # 入っていれば何もしない(冪等)


def test_setup_with_whisper_creates_venv_then_installs_requirements():
    import setup as setup_module

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        class Run(_FakeRun):
            def __call__(self, argv, **kw):
                result = super().__call__(argv, **kw)
                if "venv" in argv:                         # 偽の venv を作る
                    (tmp / "v" / "Scripts").mkdir(parents=True)
                    (tmp / "v" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
                return result

        fake = Run()
        _run_install(True, fake, tmp=tmp)
        assert len(fake.calls) == 2, fake.calls
        make_venv, pip = fake.calls
        assert make_venv[0][1:3] == ["-m", "venv"] and make_venv[0][3] == str(tmp / "v")
        assert pip[0][0] == str(tmp / "v" / "Scripts" / "python.exe")
        assert pip[0][1:5] == ["-m", "pip", "install", "-r"] and pip[0][5].endswith("requirements-whisper.txt")
        assert pip[1]["env"]["PYTHONUTF8"] == "1"          # 古い pip が日本語コメントで落ちないため
        # 既に venv があれば作り直さない
        fake2 = Run()
        (tmp / "v" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
        _run_install(True, fake2, tmp=tmp)
        assert len(fake2.calls) == 1 and "pip" in fake2.calls[0][0]
        assert setup_module.WHISPER_VENV_DIR == Path.home() / ".venvs" / "kabeuchi-stt"


def test_setup_with_whisper_failure_does_not_stop_setup():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "v" / "Scripts").mkdir(parents=True)
        (tmp / "v" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
        fake = _FakeRun(fail_on="pip")
        _run_install(True, fake, tmp=tmp)                  # 例外にならず戻る(他の機能は使える)
        assert len(fake.calls) == 1
        # 要求ファイルが無くても落ちない
        fake3 = _FakeRun()
        with tempfile.TemporaryDirectory() as td2:
            _run_install(True, fake3, tmp=Path(td2), requirements_exists=False)
        assert fake3.calls == []


def test_setup_flag_defaults_to_asking_and_whisper_requirements_file():
    import setup as setup_module

    text = (ROOT / "requirements-whisper.txt").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# -*- coding: utf-8 -*-"      # 古い pip(cp932)で日本語コメントが落ちないため
    pkgs = {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}
    bundled = (ROOT / "kabeuchi_stt" / "requirements.txt").read_text(encoding="utf-8")
    bundled_pkgs = {line.strip() for line in bundled.splitlines() if line.strip() and not line.startswith("#")}
    assert pkgs == bundled_pkgs, (pkgs, bundled_pkgs)
    assert setup_module.WHISPER_REQUIREMENTS == ROOT / "requirements-whisper.txt"
    # kabeuchi_launch が見に行く場所と、setup が作る場所は同じ
    assert Path(kl.default_python()) == setup_module.WHISPER_VENV_DIR / "Scripts" / "pythonw.exe"


# ---------------------------------------------------------------
# 公開リポジトリとしての見張り
# ---------------------------------------------------------------
def test_bundle_has_no_personal_paths_or_data_files():
    """同梱した kabeuchi_stt に、実パス・メールアドレス・本人のデータが紛れていない。"""
    import re

    bundle = ROOT / "kabeuchi_stt"
    pattern = re.compile(r"[A-Za-z]:\\Users\\(?!<|%|\$|someone)|[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
    hits = []
    for path in bundle.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".ico", ".png"):
            continue
        rel = path.relative_to(bundle)
        if rel.parts[0] in ("models", "out") or path.name in ("settings.json", "error.log", "sample_script.md") \
                or path.suffix in (".wav", ".lnk"):
            continue                                       # .gitignore で追跡されないもの(手元に在っても公開されない)
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{rel}:{number}: {line.strip()[:80]}")
    assert not hits, "\n".join(hits)
    ignore = (bundle / ".gitignore").read_text(encoding="utf-8")
    for needle in ("settings.json", "out/", "models/", "*.wav"):
        assert needle in ignore, needle


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
