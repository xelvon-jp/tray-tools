# tests/test_folder_watch.py
# フォルダ監視移動(folder_watch.py の純ロジックと、folder_watch_settings.py の
# ダイアログ)を確かめる。Qt はダイアログのテストだけ offscreen で使う。
#
#   %USERPROFILE%\.venvs\tray-tools\Scripts\python.exe tests\test_folder_watch.py
#
# 触るのは tempfile の一時フォルダと一時の settings.json だけ。本物の
# ダウンロードフォルダ・settings.json には触れない。
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import folder_watch as fw  # noqa: E402

_app = None


def _qapp():
    global _app
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    return _app


def _rule(source, dest, **extra):
    raw = {"source": str(source), "dest": str(dest)}
    raw.update(extra)
    rule = fw.normalize_rule(raw)
    assert rule is not None
    return rule


def _write(path, data=b"x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------
# 設定の正規化
# ---------------------------------------------------------------
def test_normalize_rule_defaults_and_clamps():
    rule = _rule("C:\\in", "C:\\out")
    # 除外拡張子のキーが無いときだけ、生成中ファイルの初期値が入る
    assert "crdownload" in rule["exclude_extensions"]
    assert "tmp" in rule["exclude_extensions"]
    assert rule["interval_seconds"] == fw.DEFAULT_INTERVAL_SECONDS
    assert rule["enabled"] is True
    assert _rule("C:\\in", "C:\\out", interval_seconds=1)["interval_seconds"] == 5
    assert _rule("C:\\in", "C:\\out", interval_seconds="abc")["interval_seconds"] == 30
    assert _rule("C:\\in", "C:\\out", interval_seconds=999999)["interval_seconds"] == 86400
    # 空リストは「除外しない」という指定なので初期値で上書きしない
    assert _rule("C:\\in", "C:\\out", exclude_extensions=[])["exclude_extensions"] == []
    # 書き方のゆれを受ける
    rule = _rule("C:\\in", "C:\\out", exclude_extensions=" .ZIP, tmp ", exclude_patterns="*.bak, ~$*")
    assert rule["exclude_extensions"] == ["zip", "tmp"]
    assert rule["exclude_patterns"] == ["*.bak", "~$*"]


def test_normalize_rule_rejects_empty_and_rules_dedupe():
    assert fw.normalize_rule({"source": "", "dest": "x"}) is None
    assert fw.normalize_rule({"source": "x"}) is None
    assert fw.normalize_rule("not-a-dict") is None
    # パスは / 区切りにしておく(Windows でも Linux の VM でも同じ判定になるように。
    # 大小文字の同一視は Windows 側の normcase の仕事で、ここでは末尾の区切りだけ見る)
    rules = fw.normalize_rules([
        {"source": "/in", "dest": "/out"},
        {"source": "/in/", "dest": "/out"},   # 同じ組(末尾の区切り違い)は重なりとして落とす
        {"source": "/in", "dest": "/other"},
        {"source": "", "dest": ""},
    ])
    assert len(rules) == 2


# ---------------------------------------------------------------
# 除外条件と検証
# ---------------------------------------------------------------
def test_is_excluded_extensions_and_patterns():
    rule = _rule("in", "out", exclude_extensions=["crdownload", "tmp"],
                 exclude_patterns=["~$*", "*.bak"])
    assert fw.is_excluded("file.crdownload", rule)
    assert fw.is_excluded("FILE.TMP", rule)
    assert fw.is_excluded("~$文書.docx", rule)
    assert fw.is_excluded("backup.BAK", rule)
    assert not fw.is_excluded("写真.jpg", rule)
    assert not fw.is_excluded("tmpfile", rule)        # 拡張子なしは当たらない
    assert not fw.is_excluded("file.tmp.zip", rule)   # 最後の拡張子だけを見る


def test_validate_rule_loops():
    assert fw.validate_rule(_rule("/a", "/a/")) is not None      # 同じ
    assert fw.validate_rule(_rule("/a", "/a/sub")) is not None   # 先が元の中
    assert fw.validate_rule(_rule("/a", "/b")) is None
    assert fw.validate_rule(_rule("/a/sub", "/a")) is None       # 元が先の中はループにならない


def test_unique_dest_path_numbering():
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        assert fw.unique_dest_path(dest, "a.txt") == dest / "a.txt"
        _write(dest / "a.txt")
        assert fw.unique_dest_path(dest, "a.txt") == dest / "a (1).txt"
        _write(dest / "a (1).txt")
        assert fw.unique_dest_path(dest, "a.txt") == dest / "a (2).txt"
        _write(dest / "README")
        assert fw.unique_dest_path(dest, "README") == dest / "README (1)"


# ---------------------------------------------------------------
# 監視の動き(安定判定が本命)
# ---------------------------------------------------------------
def test_scan_moves_only_after_stable():
    with tempfile.TemporaryDirectory() as tmp:
        source, dest = Path(tmp, "in"), Path(tmp, "out")
        _write(source / "a.txt", b"hello")
        watcher = fw.FolderWatcher(_rule(source, dest))
        first = watcher.scan_once()
        assert first["moved"] == [] and first["error"] is None
        assert (source / "a.txt").exists()          # 初見は記録だけ
        second = watcher.scan_once()
        assert second["moved"] == [(str(source / "a.txt"), str(dest / "a.txt"))]
        assert (dest / "a.txt").read_bytes() == b"hello"  # 先は自動で作られる


def test_scan_waits_while_growing():
    with tempfile.TemporaryDirectory() as tmp:
        source, dest = Path(tmp, "in"), Path(tmp, "out")
        target = _write(source / "big.bin", b"1")
        watcher = fw.FolderWatcher(_rule(source, dest))
        watcher.scan_once()
        with open(target, "ab") as f:
            f.write(b"2")                            # 伸びた
        assert watcher.scan_once()["moved"] == []
        assert watcher.scan_once()["moved"] != []    # 安定したら次で動く


def test_scan_skips_excluded_dirs_and_patterns():
    with tempfile.TemporaryDirectory() as tmp:
        source, dest = Path(tmp, "in"), Path(tmp, "out")
        _write(source / "dl.crdownload", b"partial")
        _write(source / "keep.bak", b"old")
        _write(source / "sub" / "inner.txt", b"nested")
        _write(source / "ok.txt", b"fine")
        rule = _rule(source, dest, exclude_patterns=["*.bak"])
        watcher = fw.FolderWatcher(rule)
        for _ in range(3):
            watcher.scan_once()
        assert (source / "dl.crdownload").exists()   # 生成中拡張子は残る
        assert (source / "keep.bak").exists()        # パターン除外も残る
        assert (source / "sub" / "inner.txt").exists()  # サブフォルダには入らない
        assert (dest / "ok.txt").exists()


def test_scan_collision_uses_numbered_name():
    with tempfile.TemporaryDirectory() as tmp:
        source, dest = Path(tmp, "in"), Path(tmp, "out")
        _write(dest / "a.txt", b"old")
        _write(source / "a.txt", b"new")
        watcher = fw.FolderWatcher(_rule(source, dest))
        watcher.scan_once()
        moved = watcher.scan_once()["moved"]
        assert moved == [(str(source / "a.txt"), str(dest / "a (1).txt"))]
        assert (dest / "a.txt").read_bytes() == b"old"      # 上書きしない
        assert (dest / "a (1).txt").read_bytes() == b"new"


def test_scan_errors_are_reported_not_raised():
    with tempfile.TemporaryDirectory() as tmp:
        missing = fw.FolderWatcher(_rule(Path(tmp, "nope"), Path(tmp, "out")))
        result = missing.scan_once()
        assert result["moved"] == [] and "見つかりません" in result["error"]
        loop = fw.FolderWatcher(_rule(tmp, Path(tmp, "sub")))
        assert loop.scan_once()["error"] is not None


def test_config_save_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "settings.json")
        path.write_text(json.dumps({"other": {"x": 1}}), encoding="utf-8")
        rules = [_rule("C:\\in", "C:\\out", interval_seconds=10)]
        app_settings = {}
        fw.save_config(str(path), {"rules": rules}, app_settings)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["other"] == {"x": 1}            # ほかのセクションは消えない
        assert app_settings["folder_watch"]["rules"] == rules
        loaded = fw.load_config(str(path))
        assert loaded["rules"] == rules


# ---------------------------------------------------------------
# ダイアログ(参照は差し替え、実フォルダは選ばない)
# ---------------------------------------------------------------
def test_settings_dialog_roundtrip():
    _qapp()
    import folder_watch_settings as fws
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "settings.json")
        source, dest = Path(tmp, "in"), Path(tmp, "out")
        source.mkdir()
        config = {"rules": [_rule(source, dest, interval_seconds=15)]}
        app_settings = {}
        dialog = fws.FolderWatchSettingsDialog(
            config, str(path), app_settings, choose_folder=lambda: "")
        assert dialog.list.count() == 1
        # 追加: 編集サブダイアログを差し替えて、値を入れたことにして足す
        made = _rule(Path(tmp, "in2"), Path(tmp, "out2"), interval_seconds=20)
        dialog._edit_rule = lambda rule: made
        assert dialog.add_rule() is True
        assert dialog.list.count() == 2
        dialog.remove_current()
        assert dialog.list.count() == 1
        dialog.accept()
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert len(stored["folder_watch"]["rules"]) == 1
        assert stored["folder_watch"]["rules"][0]["interval_seconds"] == 15
        assert app_settings["folder_watch"]["rules"][0]["source"] == str(source)
        dialog.deleteLater()


def test_rule_edit_dialog_validation():
    _qapp()
    import folder_watch_settings as fws
    with tempfile.TemporaryDirectory() as tmp:
        dialog = fws.RuleEditDialog({}, choose_folder=lambda: "")
        dialog.source.setText(str(tmp))
        dialog.dest.setText(str(Path(tmp, "sub")))   # 先が元の中
        # 警告ダイアログは offscreen では閉じられないので、accept は叩かず
        # 「この値では検証に落ちる」ことだけ確かめる(検証そのものは上のテストで担保)
        assert fw.validate_rule(dialog.values()) is not None
        dialog.dest.setText(str(Path(tmp, "elsewhere")))
        dialog.accept()
        assert dialog.result() == 1                  # QDialog.Accepted == 1
        assert dialog.values()["interval_seconds"] >= fw.MIN_INTERVAL_SECONDS
        dialog.deleteLater()


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
