# test_unified_picker.py
# 統合ピッカー(unified_picker.py)の候補生成・動的候補・プレビュー文と、
# picker.PickerWindow の絞り込み名(3要素タプル)対応のテスト。
#
# 表示ルール(2026-11-11): 静的候補は登録+ピン留めだけ(フォルダ最上位)。
# 機能・アプリは入力に `*` が入ったときだけ動的候補として広げて出す。
# 動的候補は窓の dynamic_last で登録の下に出る。
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import command_launcher as cl
import command_palette as cp
import picker as picker_mod
import unified_picker as up

WEB = [{"name": "Google", "url": "https://www.google.com/search?q={query}"}]


def _settings(tmp):
    # 登録順はわざと種別を混ぜてある。表示は フォルダ→URL/URI→実行 の順に
    # 並び替わることを確かめるため。
    return {"launcher": {"bookmarks": [
        {"name": "道具", "path": r"C:\tools\app.exe", "params": "/open %param%"},
        {"name": "ページ", "path": "https://example.com/"},
        {"name": "作業", "path": tmp},
        {"name": "ノート", "path": "onenote:https://example.com/p"},
        {"name": "電卓もどき", "path": r"C:\tools\calc.exe"},
    ]}}


def _apps():
    return [cp.Candidate("app", "メモ帳", "", {"path": r"C:\apps\notepad.lnk"}),
            cp.Candidate("app", "Excel", "", {"path": r"C:\apps\excel.lnk"})]


def _dynamics(entries, **kwargs):
    args = {"entries": entries, "features": [], "apps": [],
            "web_searches": [], "file_search": None}
    args.update(kwargs)
    return up.make_dynamic_items(**args)


# ---------------------------------------------------------------
# picker の絞り込み名と動的候補の位置
# ---------------------------------------------------------------
def test_split_item():
    assert picker_mod.split_item(("名前", 1)) == ("名前", 1, "名前")
    assert picker_mod.split_item(("📁 名前", 1, "名前")) == ("📁 名前", 1, "名前")


def test_picker_match_name():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    items = [("📁 仕事フォルダ", {"kind": "jump"}, "仕事フォルダ"),
             ("🌐 例サイト", {"kind": "open"}, "例サイト")]
    window = picker_mod.PickerWindow("test", items, lambda n, d: None)
    try:
        window.search.setText("仕事")
        assert window.list.count() == 1
        window.search.setText("例")
        assert window.list.count() == 1
        window.search.setText("")
        assert window.list.count() == 2
    finally:
        window.close()


def test_picker_dynamic_last():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    items = [("📁 登録", {"kind": "jump"}, "登録")]
    dynamics = lambda text: [("🧮 計算", {"kind": "calc"})]  # noqa: E731
    window = picker_mod.PickerWindow(
        "test", items, lambda n, d: None, dynamic_items=dynamics,
        dynamic_last=True)
    try:
        texts = [window.list.item(i).text() for i in range(window.list.count())]
        assert texts == ["📁 登録", "🧮 計算"], texts
    finally:
        window.close()
    window = picker_mod.PickerWindow(
        "test", items, lambda n, d: None, dynamic_items=dynamics)
    try:
        texts = [window.list.item(i).text() for i in range(window.list.count())]
        assert texts == ["🧮 計算", "📁 登録"], texts
    finally:
        window.close()


# ---------------------------------------------------------------
# 静的候補(登録+ピン留めだけ。フォルダが最上位)
# ---------------------------------------------------------------
def test_build_items():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        items = up.build_items(
            entries, pinned_features=[("capture", "キャプチャ")], add_path=tmp)
        matches = [m for _d, _p, m in items]
        # 登録は フォルダ→URL/URI→実行 の順(登録順は種別混在でも)
        assert matches[:5] == ["作業", "ページ", "ノート", "道具", "電卓もどき"], \
            matches
        by_match = {match: (display, payload)
                    for display, payload, match in items if match}
        assert by_match["作業"][0] == "📁 作業"
        assert by_match["作業"][1]["kind"] == "jump"
        assert by_match["ページ"][0] == "🌐 ページ"
        assert by_match["ページ"][1]["via"] == "url"
        assert by_match["ノート"][0] == "📓 ノート"
        assert by_match["ノート"][1]["via"] == "onenote"
        # パラメタ付きは ▶(コマンド)、無しは 🚀(アプリ)
        assert by_match["道具"][0] == "▶ 道具"
        assert by_match["道具"][1]["kind"] == "exec"
        assert by_match["電卓もどき"][0] == "🚀 電卓もどき"
        # ピン留めした機能だけが静的候補に出る
        assert by_match["キャプチャ"][0] == "🔧 キャプチャ"
        assert by_match["キャプチャ"][1] == {
            "kind": "feature", "item_key": "capture", "name": "キャプチャ",
            "pinned": True}
        # 控え(機能の全体・アプリ)は静的候補には出ない
        assert "メモ帳" not in by_match and "Excel" not in by_match
        builtin_names = [m for m in by_match
                         if by_match[m][1].get("kind") == "builtin"]
        assert sorted(builtin_names) == ["Command", "Config", "Reload"]
        kinds = [payload.get("kind") for _d, payload, _m in items]
        assert "add" in kinds and "import" in kinds


def test_entry_payload():
    entry = cl.Entry("郵便", "mailto:a@example.com", kind=cl.KIND_URI)
    payload = up.entry_payload(entry)
    assert payload["kind"] == "open" and payload["via"] == "uri"


def test_resolve_input():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        payload = up.resolve_input("道具;xyz", entries)
        assert payload["kind"] == "exec" and payload["param"] == "xyz"
        assert up.resolve_input("道", entries) is None  # 前方一致では拾わない
        assert up.resolve_input("知らない", entries) is None
        assert up.resolve_input("", entries) is None


# ---------------------------------------------------------------
# 動的候補
# ---------------------------------------------------------------
def test_dynamics_calc_and_web():
    dynamics = _dynamics([], web_searches=WEB)
    rows = dynamics("1+2")
    assert rows[0][1]["kind"] == "calc" and rows[0][1]["value"] == "3"
    assert rows[0][0].startswith("🧮")
    assert any(p["kind"] == "web" for _d, p in rows)


def test_dynamics_exec_semicolon():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        dynamics = _dynamics(entries)
        rows = dynamics("道具;abc")
        exec_rows = [p for _d, p in rows if p.get("kind") == "exec"]
        assert exec_rows and exec_rows[0]["param"] == "abc"


def test_dynamics_empty_input():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        dynamics = _dynamics(entries, apps=_apps())
        assert dynamics("") == []  # 空入力では何も足さない(静的候補だけ)


def test_dynamics_pool_hidden_without_star():
    dynamics = _dynamics([], features=[("capture", "キャプチャ")],
                         apps=_apps())
    rows = dynamics("メモ")
    assert not any(p.get("kind") == "app" for _d, p in rows)
    rows = dynamics("キャプ")
    assert not any(p.get("kind") == "feature" for _d, p in rows)


def test_dynamics_widen_star_suffix():
    # Exe* で、スタメンにいない Excel が候補に出る(Nagato の期待した形)
    dynamics = _dynamics([], apps=_apps())
    rows = dynamics("Exe*")
    app_rows = [p for _d, p in rows if p.get("kind") == "app"]
    assert app_rows and app_rows[0]["name"] == "Excel"
    assert app_rows[0]["pool"] is True
    rows = dynamics("exe*")  # 大文字小文字は無視
    assert any(p.get("name") == "Excel" for _d, p in rows)


def test_dynamics_widen_star_prefix_contains():
    dynamics = _dynamics([], apps=_apps())
    rows = dynamics("*cel")  # 先頭の * は部分一致
    assert any(p.get("name") == "Excel" for _d, p in rows)
    rows = dynamics("*メモ")
    assert any(p.get("name") == "メモ帳" for _d, p in rows)


def test_dynamics_widen_star_all_and_order():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        dynamics = _dynamics(
            entries, features=[("capture", "キャプチャ"),
                               ("memo", "メモ機能")],
            apps=_apps(), pinned_keys=("capture",))
        rows = dynamics("*")
        kinds = [p.get("kind") for _d, p in rows]
        # 登録 → ピン留め機能 → 控えの機能 → アプリ の順
        assert kinds.index("jump") < kinds.index("feature")
        feature_rows = [p for _d, p in rows if p.get("kind") == "feature"]
        assert feature_rows[0]["item_key"] == "capture"
        assert feature_rows[0]["pinned"] is True
        assert feature_rows[0]["pool"] is False
        assert feature_rows[1]["item_key"] == "memo"
        assert feature_rows[1]["pool"] is True
        assert any(p.get("kind") == "app" for _d, p in rows)
        # 登録の中もフォルダが先
        jump_at = kinds.index("jump")
        open_at = kinds.index("open")
        assert jump_at < open_at


def test_dynamics_widen_registered_prefix():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        dynamics = _dynamics(entries, apps=_apps())
        rows = dynamics("作*")
        assert any(p.get("kind") == "jump" and p.get("name") == "作業"
                   for _d, p in rows)


def test_dynamics_fuzzy_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        settings = {"launcher": {"bookmarks": [{"name": "Google", "path": tmp}]}}
        entries = cl.build_entries(settings)
        dynamics = _dynamics(entries, apps=_apps())
        rows = dynamics("ggl")
        assert any(p.get("kind") == "jump" for _d, p in rows)
        # あいまい一致の対象は登録だけ(アプリの Excel 等は出ない)
        rows = dynamics("exl")
        assert not any(p.get("kind") == "app" for _d, p in rows)
        # 前方一致があるときは、あいまいの行は出さない
        rows = dynamics("Goo")
        assert not any(d == "📁 Google" for d, _p in rows)


def test_dynamics_file_search():
    dynamics = _dynamics([], file_search=lambda q: [r"C:\docs\report.txt"])
    rows = dynamics("repo")
    file_rows = [p for _d, p in rows if p.get("kind") == "file"]
    assert file_rows and file_rows[0]["path"] == r"C:\docs\report.txt"
    assert any(d.startswith("📄") for d, _p in rows)


# ---------------------------------------------------------------
# プレビュー文と Tab
# ---------------------------------------------------------------
def test_preview_text():
    assert "C:\\x" in up.preview_text({"kind": "jump", "path": r"C:\x"})
    assert "42" in up.preview_text({"kind": "calc", "value": "42"})
    assert "ブラウザ" in up.preview_text(
        {"kind": "open", "target": "https://e.com", "via": "url"})
    assert "OneNote" in up.preview_text(
        {"kind": "open", "target": "onenote:x", "via": "onenote"})
    assert up.preview_text({"kind": "builtin", "id": "config"}) != ""
    entry = cl.Entry("道具", r"C:\tools\app.exe", "/open %param%",
                     kind=cl.KIND_FILE)
    assert "app.exe" in up.preview_text({"kind": "exec", "entry": entry})
    assert up.preview_text({"kind": "unknown"}) == ""
    assert up.preview_text("not-a-dict") == ""


def test_tab_text():
    tab = up.make_tab_text()
    with tempfile.TemporaryDirectory() as tmp:
        result = tab("x", {"kind": "jump", "path": tmp})
        assert result is not None and result.endswith("\\")
    assert tab("x", {"kind": "exec", "name": "道具"}) == "道具;"
    assert tab("x", {"kind": "feature", "item_key": "capture"}) is None
    result = tab("x", {"kind": "file", "path": "/docs/report.txt"})
    assert result == "/docs\\"


def test_build_items_filer_context():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        items = up.build_items(entries, add_path=tmp,
                               stash_count=3, show_import=False)
        assert items[0][1] == {"kind": "stash"}
        assert items[0][0].startswith("🧺 一時置き場（3件）")
        assert items[0][2] == "一時置き場"
        assert not any(p.get("kind") == "import" for _d, p, _m in items)


def test_preview_filer_context():
    text = up.preview_text({"kind": "jump", "path": r"C:\x"}, context="filer")
    assert "アクティブな窓" in text and r"C:\x" in text
    assert "一時置き場" in up.preview_text({"kind": "stash"}, context="filer")


def test_call_signatures_match():
    """feature_screen / filer が呼ぶ部品の関数が、実在し引数の形も合うこと。

    統合の配線は窓まわりで実機でしか動かせない部分が多く、存在しない関数や
    引数の渡し間違いが潜りやすい(第1段で実際に踏んだ)。ここは静的に守る。"""
    import ast
    import inspect
    import pathlib

    import command_palette as cp_mod
    import launcher as launcher_mod

    modules = {"launcher": launcher_mod, "command_launcher": cl,
               "command_palette": cp_mod, "unified_picker": up}
    root = pathlib.Path(__file__).resolve().parent.parent
    for filename in ("feature_screen.py", "filer.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in modules):
                continue
            module = modules[func.value.id]
            target = getattr(module, func.attr, None)
            assert target is not None, \
                f"{filename}: {func.value.id}.{func.attr} が存在しない"
            if not callable(target):
                continue
            try:
                sig = inspect.signature(target)
            except (TypeError, ValueError):
                continue
            params = [p for p in sig.parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            required = [p.name for p in params if p.default is p.empty]
            given_kw = {kw.arg for kw in node.keywords if kw.arg}
            assert len(node.args) <= len(params), \
                f"{filename}: {func.value.id}.{func.attr} の位置引数が多すぎる"
            missing = [name for name in required[len(node.args):]
                       if name not in given_kw]
            assert not missing, \
                f"{filename}: {func.value.id}.{func.attr} に {missing} が渡っていない"


def test_filer_j_unified_flow():
    """あっふの J(cmd_bookmark_jump)が統合窓の候補で動き、登録・削除・並べ替えが
    settings.json に効くこと。FilerWindow 本体は Windows 専用 API で VM では
    起動できないので、必要な属性だけを持つ代役でメソッド単体を駆動する。"""
    import json
    import types
    from pathlib import Path

    try:
        import filer
    except Exception:  # noqa: BLE001  filer は Windows 専用 API を読み込むため
        return  # 読み込めない環境(VM)では飛ばす。Windows の CI で効く
    import launcher

    class FakeSearch:
        def text(self):
            return ""

    class FakePicker:
        def __init__(self):
            self.search = FakeSearch()
            self.items = []
            self.selected = None

        def set_items(self, items):
            self.items = items

        def select_data(self, data, name=None):
            self.selected = (data, name)
            return True

    with tempfile.TemporaryDirectory() as tmp:
        left = Path(tmp, "left")
        right = Path(tmp, "right")
        left.mkdir()
        right.mkdir()
        settings_path = Path(tmp, "settings.json")
        stored = {"launcher": {"bookmarks": [
            {"name": "A", "path": str(right)},
            {"name": "B", "path": str(left)},
        ]}}
        settings_path.write_text(json.dumps(stored), encoding="utf-8")

        captured = {}
        picker = FakePicker()

        stub = types.SimpleNamespace()
        stub.settings_path = str(settings_path)
        stub.app_settings = {}
        stub.stash_paths = []
        stub.jumped = []
        stub.logs = []
        stub.stash_shown = []
        stub.jump_to = lambda path: stub.jumped.append(path)
        stub.log = lambda message: stub.logs.append(message)
        stub.confirm = lambda *a, **k: True
        stub._unified_apps = lambda: []
        stub._unified_filer_file_search = lambda: (lambda query: [])
        stub.active = types.SimpleNamespace(
            real_dir=lambda: str(left),
            show_stash=lambda: stub.stash_shown.append(True))

        def fake_open_picker(title, items, on_path, on_accept=None, **kwargs):
            captured.update(title=title, items=items, accept=on_accept,
                            delete=kwargs.get("on_delete"),
                            move=kwargs.get("on_move"))
            picker.items = items
            return picker

        stub._open_picker = fake_open_picker
        filer.FilerWindow.cmd_bookmark_jump(stub)

        items = captured["items"]
        assert items[0][1] == {"kind": "stash"}
        matches = [m for _n, _d, m in items]
        assert matches[1:3] == ["A", "B"], matches
        assert not any(p.get("kind") == "import" for _n, p, _m in items)

        def row(match):
            return next(e for e in picker.items if e[2] == match)

        # フォルダの行 → アクティブ窓の移動(jump_to)
        captured["accept"](*row("B")[:2])
        assert stub.jumped == [str(left)]
        # 一時置き場の行
        captured["accept"](*items[0][:2])
        assert stub.stash_shown == [True]
        # 「ここを登録」→ settings に増える
        original = launcher._ask_name
        launcher._ask_name = lambda parent, default: "C_new"
        try:
            add_row = next(e for e in picker.items
                           if isinstance(e[1], dict) and e[1].get("kind") == "add")
            captured["accept"](add_row[0], add_row[1])
        finally:
            launcher._ask_name = original
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert [b["name"] for b in saved["launcher"]["bookmarks"]] == \
            ["A", "B", "C_new"]
        # 並べ替えと削除(ピッカー側の一覧は set_items で組み直される)
        captured["move"](*row("B")[:2], -1)
        captured["delete"](*row("A")[:2])
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert [b["name"] for b in saved["launcher"]["bookmarks"]] == \
            ["B", "C_new"]


ALL_TESTS = [test_split_item, test_picker_match_name, test_picker_dynamic_last,
             test_build_items, test_entry_payload, test_resolve_input,
             test_dynamics_calc_and_web, test_dynamics_exec_semicolon,
             test_dynamics_empty_input, test_dynamics_pool_hidden_without_star,
             test_dynamics_widen_star_suffix,
             test_dynamics_widen_star_prefix_contains,
             test_dynamics_widen_star_all_and_order,
             test_dynamics_widen_registered_prefix,
             test_dynamics_fuzzy_fallback, test_dynamics_file_search,
             test_preview_text, test_tab_text, test_build_items_filer_context,
             test_preview_filer_context, test_call_signatures_match,
             test_filer_j_unified_flow]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
