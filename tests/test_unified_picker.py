"""統合ピッカー(モード式)の候補づくり・動的候補・ピッカー窓のモード動作のテスト。

`[` フォルダ選択 / `]` フォルダヒストリ / `!` コマンド・ランチャ /
`?` 検索(補完なし) / `=` 電卓 の5モード。入力欄の左端のチップがモードを表し、
チップが無い状態ではモード切替のヒント行が出る。`*` はワイルドカード。
"""
import os
import subprocess  # noqa: F401  msvcrt のスタブより先に読み切る(run_filer_tests と同じ)
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# filer を import するテスト用に、Windows 専用の ctypes 入口だけ足す
# (run_filer_tests.py と同じ流儀。実物の ctypes 本体はそのまま使う)。
import ctypes as _ct
import types as _types

if not hasattr(_ct, "WinDLL"):
    class _WinDLL:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: 0

    _ct.WinDLL = _WinDLL
if not hasattr(_ct, "WINFUNCTYPE"):
    _ct.WINFUNCTYPE = lambda *a, **k: (lambda f: f)  # noqa: E731
if not hasattr(_ct, "windll"):
    _ct.windll = _types.SimpleNamespace()
sys.modules.setdefault("msvcrt", _types.ModuleType("msvcrt"))

import command_launcher as cl
import command_palette as cp
import picker as picker_mod
import unified_picker as up


def _settings(tmp_path):
    return {
        "launcher": {"bookmarks": [
            {"name": "資料", "path": str(tmp_path / "docs")},
            {"name": "電卓アプリ", "path": "calc.exe"},
            {"name": "メモ帳", "path": "notepad.exe"},
            {"name": "ページ", "path": "https://example.com/"},
        ]},
        "command_palette": {"features": {}, "web_searches": {
            "Google": "https://www.google.com/search?q={q}",
        }},
    }


def _entries(tmp_path):
    # 資料 のフォルダは実在しないと、種別の判定でファイル扱いになる。
    (tmp_path / "docs").mkdir(exist_ok=True)
    return cl.build_entries(_settings(tmp_path))


def _kinds(rows):
    return [row[1].get("kind") for row in rows]


def _names(rows):
    # 絞り込み名(3要素目)。無ければ表示名。
    return [row[2] if len(row) > 2 else row[0] for row in rows]


def test_split_item():
    # 項目は (表示名, データ) が基本。3つ目に絞り込み名を足せる。
    assert picker_mod.split_item(("名前", 1)) == ("名前", 1, "名前")
    assert picker_mod.split_item(("📁 名前", 1, "名前")) == ("📁 名前", 1, "名前")


def test_picker_match_name():
    # 統合ピッカーは前方一致(部分一致のフォールバックはピッカー側の作法)。
    # キーワードは1つの文字列として頭から照合する(空白込み)。
    assert picker_mod.name_matches("メモ帳", "メモ")
    assert picker_mod.name_matches("メモ帳", "")
    assert not picker_mod.name_matches("メモ帳", "帳")
    assert not picker_mod.name_matches("メモ帳", "電卓")
    assert picker_mod.name_matches("Google 検索", "google 検")
    assert not picker_mod.name_matches("Google 検索", "go 検索")


def test_wildcard_match():
    # `*` / `?` を含むキーワードだけワイルドカード照合になる。
    assert picker_mod.name_matches("メモ帳.txt", "*.txt")
    assert not picker_mod.name_matches("メモ帳.txt", "*.md")
    assert picker_mod.name_matches("メモ帳.txt", "メモ*")
    assert picker_mod.name_matches("abc def", "a*f")
    # ワイルドカードが無ければ従来どおり前方一致(? はワイルドカード扱い)
    assert picker_mod.name_matches("abc", "ab")
    assert not picker_mod.name_matches("abc", "bc")
    assert picker_mod.name_matches("abc", "a?c")


def test_entry_payload():
    assert up.entry_payload(cl.Entry("資料", r"C:\docs", kind="folder")) == \
        {"kind": "jump", "path": r"C:\docs", "name": "資料", "bookmark": True}
    assert up.entry_payload(cl.Entry("ページ", "https://x/", kind="url")) == \
        {"kind": "open", "target": "https://x/", "name": "ページ",
         "via": "url", "bookmark": True}
    entry = cl.Entry("メモ帳", "notepad.exe")
    payload = up.entry_payload(entry)
    assert payload["kind"] == "exec" and payload["entry"] is entry
    assert payload["name"] == "メモ帳" and payload["bookmark"] is True


def test_mode_items_folder(tmp_path):
    rows = up.build_mode_items("[", _entries(tmp_path))
    # `[` にはフォルダの登録だけ。Web・プログラムは `!` の側。
    # (お気に入り取り込みの行は、文脈が無ければ付く)
    assert _kinds(rows) == ["jump", "import"], rows
    assert rows[0][1]["path"] == str(tmp_path / "docs")
    # 追加の行: スタッシュ → 登録 → 登録 → 読み込み(ファイルの文脈だけ)
    rows = up.build_mode_items("[", _entries(tmp_path),
                               add_path=str(tmp_path / "here"),
                               stash_count=2, show_import=True)
    assert _kinds(rows) == ["stash", "jump", "add", "import"], rows
    assert rows[2][1] == {"kind": "add", "path": str(tmp_path / "here")}
    assert rows[3][1] == {"kind": "import"}
    # 文脈が無い常駐側では import は出ない。スタッシュは件数を渡したときだけ
    # (0件でも行は出る。あっふ側は常に渡す)。
    rows = up.build_mode_items("[", _entries(tmp_path),
                               add_path=str(tmp_path / "here"),
                               stash_count=0, show_import=False)
    assert _kinds(rows) == ["stash", "jump", "add"], rows


def test_mode_items_history(tmp_path):
    recent = [r"C:\a", r"C:\b", r"C:\c"]
    rows = up.build_mode_items("]", _entries(tmp_path), recent=recent)
    assert _kinds(rows) == ["jump", "jump", "jump"], rows
    assert [row[1]["path"] for row in rows] == recent
    # 履歴が無ければ空(登録フォルダは混ぜない)
    assert up.build_mode_items("]", _entries(tmp_path)) == []


def test_mode_items_command(tmp_path):
    features = [("calculator", "電卓"), ("filer", "あっふ")]
    rows = up.build_mode_items("!", _entries(tmp_path), features=features,
                               pinned_keys=["filer"])
    # 登録(URL/URI → 実行) → ピン留め機能 → 残りの機能 → 内蔵コマンド
    assert _kinds(rows) == ["open", "exec", "exec", "feature", "feature",
                            "builtin", "builtin", "builtin"], rows
    names = _names(rows)
    assert names[:3] == ["ページ", "電卓アプリ", "メモ帳"], names
    assert names[3] == "あっふ" and rows[3][1]["pinned"] is True
    # ピン留めした機能は、ピン留めの段にだけ出る(全機能の段では重ねない)
    assert names[4] == "電卓", names
    assert names[5:] == ["Command", "Config", "Reload"], names
    # ピン留めが無ければ、ピン留めの段は無い
    rows = up.build_mode_items("!", _entries(tmp_path), features=features)
    assert _kinds(rows) == ["open", "exec", "exec", "feature", "feature",
                            "builtin", "builtin", "builtin"], rows
    assert _names(rows)[3:5] == ["電卓", "あっふ"]


def test_mode_items_search_and_calc_are_empty():
    # `?` の静的候補は無し(打った語への動的候補だけ)。`=` は窓を開くので候補なし。
    assert up.build_mode_items("?", []) == []
    assert up.build_mode_items("=", []) == []
    assert up.build_mode_items(None, []) == []


def test_recent_folders():
    settings = {"filer": {"recent": ["/a", "/b", 3, "/c"]}}
    assert up.recent_folders(settings) == ["/a", "/b", "/c"]
    assert up.recent_folders({}) == []
    assert up.recent_folders({"filer": {}}) == []


def test_dynamics_folder_mode(tmp_path):
    # `[` でパスを打てば、そのフォルダへ跳ぶ行が出る。パスの解釈
    # (launcher.typed_items)は Windows 形式のパス専用なので、ここでは
    # 変換の配線だけを差し替えて確かめる(実パスの解決は実機側)。
    state = {"mode": "[", "apps_expanded": False}
    dyn = up.make_mode_dynamics(entries=_entries(tmp_path), apps=[],
                                web_searches=None, file_search=None,
                                state=state)
    real = up.launcher.typed_items
    up.launcher.typed_items = lambda text: (
        [("📁   C:/docs", ("jump", "C:/docs"))] if text == "C:/docs" else [])
    try:
        rows = dyn("C:/docs")
        assert dyn("zzz-no-such") == []
    finally:
        up.launcher.typed_items = real
    assert _kinds(rows) == ["jump"], rows
    assert rows[0][1] == {"kind": "jump", "path": "C:/docs", "typed": True}


def test_dynamics_command_apps(tmp_path):
    apps = [cp.Candidate("app", "Alpha Tool", "", {"path": "C:/alpha.exe"}),
            cp.Candidate("app", "Beta", "", {"path": "C:/beta.exe"})]
    state = {"mode": None, "apps_expanded": False}
    dyn = up.make_mode_dynamics(entries=_entries(tmp_path), apps=apps,
                                web_searches=None, file_search=None,
                                state=state)
    # モードが無ければ動的候補は出ない
    assert dyn("alpha") == []
    state["mode"] = "!"
    # `!`: 未登録アプリは「探す」行だけ。名前で絞っても行は1つ
    rows = dyn("alpha")
    assert _kinds(rows) == ["more_apps"], rows
    assert rows[0][1] == {"kind": "more_apps"}
    # 広げたら、その場にアプリの行が出る(「探す」行は消える)
    state["apps_expanded"] = True
    rows = dyn("alpha")
    assert _kinds(rows) == ["app"] and rows[0][1]["name"] == "Alpha Tool", rows
    # ワイルドカードはアプリの名前に対して効く
    rows = dyn("*a*")
    assert _kinds(rows) == ["app", "app"], rows
    # 検索モードではアプリは出ない
    state["mode"] = "?"
    assert dyn("alpha") == []


def test_dynamics_search_mode(tmp_path):
    state = {"mode": "?", "apps_expanded": False}
    seen = []

    def file_search(query):
        seen.append(query)
        return ["C:/hit.txt"]

    dyn = up.make_mode_dynamics(
        entries=_entries(tmp_path), apps=[],
        web_searches=[{"name": "Google",
                       "url": "https://www.google.com/search?q={query}"}],
        file_search=file_search, state=state)
    rows = dyn("天気")
    assert _kinds(rows) == ["file", "web"], rows
    assert seen == ["天気"]
    assert rows[0][1] == {"kind": "file", "path": "C:/hit.txt"}
    assert rows[1][1]["url"] == \
        "https://www.google.com/search?q=%E5%A4%A9%E6%B0%97"
    assert rows[1][1]["service"] == "Google"
    # `?` には電卓の行は無い(式を打っても計算しない)
    assert all(d.get("kind") != "calc" for _, d in dyn("1+2"))
    # 他のモードでは検索の動的候補は出ない
    state["mode"] = "["
    assert dyn("天気") == []


def test_dynamics_folder_fuzzy_scoped(tmp_path):
    # `[` のあいまい一致はフォルダの登録だけに効く(Web・プログラムは出ない)。
    # 前方一致が1件でもあれば、動的候補には出さない(静的候補に出ている)。
    state = {"mode": "[", "apps_expanded": False}
    dyn = up.make_mode_dynamics(entries=_entries(tmp_path), apps=[],
                                web_searches=None, file_search=None,
                                state=state)
    rows = dyn("料")
    assert _kinds(rows) == ["jump"], rows
    assert rows[0][1]["name"] == "資料"
    assert dyn("資料") == []
    assert dyn("ページ") == []
    assert dyn("メモ") == []


def test_preview_text():
    assert up.preview_text({"kind": "more_apps"}) == \
        "Enter で未登録のアプリも一覧に加えます"
    # 固定の引数は出る。`;引数` の案内は無い(仕組みごと削除)。
    entry = cl.Entry("ツール", "tool.exe", params="/open")
    text = up.preview_text({"kind": "exec", "entry": entry})
    assert text == "Enter でコマンドを実行します: tool.exe /open", text
    assert "引数" not in text, text
    entry = cl.Entry("メモ帳", "notepad.exe")
    assert up.preview_text({"kind": "exec", "entry": entry}) == \
        "Enter でコマンドを実行します: notepad.exe"
    assert up.preview_text({"kind": "app", "path": "C:/a.exe"}) == \
        "Enter でアプリを起動します: C:/a.exe"


def test_tab_text():
    state = {"mode": "!", "apps_expanded": False}
    make = up.make_tab_text(state)
    # Tab はフォルダ行だけ掘り下げる(パス+区切り)。`名前;` の整形は無い。
    assert make("資料", {"kind": "jump", "path": "C:/docs"}) == "C:/docs\\"
    assert make("電卓", {"kind": "feature"}) is None
    assert make("Alpha", {"kind": "app", "name": "Alpha"}) is None
    # 検索モードでは Tab の掘り下げはしない
    state["mode"] = "?"
    assert make("資料", {"kind": "jump", "path": "C:/docs"}) is None


def test_picker_dynamic_last():
    import launcher

    _qapp()
    opened = []
    window = picker_mod.PickerWindow(
        "テスト", [("A", {"kind": "open", "target": "a"})],
        lambda target: opened.append(target),
        dynamic_items=lambda text: [("D", {"kind": "open", "target": "d"})],
        dynamic_last=True)
    try:
        window.show()
        window.search.setText("x")  # フィルタに掛からない語でも動的候補は末尾に出る
        texts = [window.list.item(i).text()
                 for i in range(window.list.count())]
        assert texts == ["D"], texts
    finally:
        window.close()
    # 履歴は command_launcher 側の既存の仕組みを使う(重複実装を足さない)
    assert hasattr(cl, "load_history")


# ---- ピッカー窓のモード動作(チップ・ヒント行・BS の段階動作・= で電卓) ----

def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _key(window, key, text=""):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.NoModifier, text)
    return window.eventFilter(window.search, event)


def test_picker_window_modes():
    from PySide6.QtCore import Qt

    _qapp()
    changes = []
    calc = []

    def on_mode_change(char):
        changes.append(char)
        if char == "[":
            window.set_items([("フォルダ1", {"kind": "jump", "path": "C:/a"})])
        elif char == "!":
            window.set_items([("機能1", {"kind": "feature", "item_key": "x"})])

    window = picker_mod.PickerWindow(
        "統合ピッカー", [("フォルダ1", {"kind": "jump", "path": "C:/a"})],
        lambda path: None,
        modes=up.MODES, initial_mode="[", on_mode_change=on_mode_change,
        on_calc=lambda expr: calc.append(expr))
    try:
        window.show()
        # 開いた直後は `[` モード。チップが見え、入力欄は空。
        assert window.current_mode() == "["
        assert window._chip.isVisible()
        assert window.search.text() == ""
        # 予約文字を打つとモードが切り替わる(文字としては入らない)
        assert _key(window, Qt.Key_Exclam, "!") is True
        assert window.current_mode() == "!"
        assert window.search.text() == ""
        assert window.list.item(0).text() == "機能1"
        # BS(入力が空)でチップが外れ、モード切替のヒント行が出る
        assert _key(window, Qt.Key_Backspace) is True
        assert window.current_mode() is None
        texts = [window.list.item(i).text()
                 for i in range(window.list.count())]
        assert texts == [f"{c} {n}モード" for c, n, _ in up.MODES], texts
        # ヒント行を選ぶと、そのモードに入る
        window.list.setCurrentRow(0)
        window._commit()
        assert window.current_mode() == "["
        assert changes == ["!", None, "["], changes
        # `=` はその場で電卓へ。ピッカーは閉じる
        assert _key(window, Qt.Key_Backspace) is True
        window.search.setText("100+50")
        # 入力が残っている間は、モードは付かないまま
        assert window.current_mode() is None
        window.search.setText("")
        assert _key(window, Qt.Key_Equal, "=") is True
        assert calc == [""], calc
        assert not window.isVisible()
    finally:
        window.close()


def test_picker_window_mode_hint_filter():
    _qapp()
    window = picker_mod.PickerWindow(
        "統合ピッカー", [], lambda path: None,
        modes=up.MODES, initial_mode=None)
    try:
        window.show()
        # モード名で絞れる
        window.search.setText("電卓")
        texts = [window.list.item(i).text()
                 for i in range(window.list.count())]
        assert texts == ["= 電卓モード"], texts
    finally:
        window.close()


def test_picker_window_command_mode_more_apps_row():
    """`!` の窓で、一番下に「未登録アプリも探す」の行が実際に出ること。

    動的候補は (表示名, データ) の2要素が窓側との約束。3要素で渡すと
    窓側が形違いとして丸ごと捨てるので、作り手と窓を繋いで確かめる。"""
    _qapp()
    state = {"mode": "!", "apps_expanded": False}
    dyn = up.make_mode_dynamics(entries=[], apps=[], web_searches=None,
                                file_search=None, state=state)
    assert all(len(row) == 2 for row in dyn("x"))
    window = picker_mod.PickerWindow(
        "統合ピッカー", [], lambda *_a: None,
        modes=up.MODES, initial_mode="!",
        dynamic_items=dyn, dynamic_last=True)
    try:
        window.show()
        texts = [window.list.item(i).text()
                 for i in range(window.list.count())]
        assert texts == [up.MORE_APPS_LABEL], texts
    finally:
        window.close()


def test_picker_window_ctrl_enter_keeps_open():
    from PySide6.QtCore import Qt

    _qapp()
    seen = []
    window = picker_mod.PickerWindow(
        "統合ピッカー",
        [("フォルダ1", {"kind": "jump", "path": "C:/a"})],
        lambda path: None,
        modes=up.MODES, initial_mode="[",
        on_ctrl_enter=lambda name, data: seen.append((name, data)))
    try:
        window.show()
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Return,
                          Qt.ControlModifier, "\r")
        assert window.eventFilter(window.search, event) is True
        assert seen == [("フォルダ1", {"kind": "jump", "path": "C:/a"})], seen
        # Ctrl+Enter ではピッカーは閉じない(メニューを重ねる側の作法)
        assert window.isVisible()
    finally:
        window.close()


def test_filer_j_unified_flow():
    """あっふの J(cmd_bookmark_jump)が統合ピッカー(モード式)の部品で組まれ、
    候補の確定で移動・登録・並べ替え・削除が settings に効くこと。

    FilerWindow 本体は窓を作らず、必要な属性だけを持つ代役で駆動する。"""
    import inspect
    import json
    import tempfile
    import types
    from pathlib import Path

    import filer
    import launcher

    source = inspect.getsource(filer.FilerWindow.cmd_bookmark_jump)
    assert "unified_picker.build_mode_items" in source
    assert "command_launcher.build_entries" in source
    assert "make_tab_text" in source
    assert "unified_picker.MODES" in source

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
        here = Path(tmp, "here")
        left.mkdir()
        right.mkdir()
        here.mkdir()
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
        stub.recent = []
        stub.jumped = []
        stub.logs = []
        stub.stash_shown = []
        stub.jump_to = lambda path: stub.jumped.append(path)
        stub.log = lambda message: stub.logs.append(message)
        stub.confirm = lambda *a, **k: True
        stub._unified_apps = lambda: []
        stub._unified_filer_file_search = lambda: (lambda query: [])
        stub._open_picker_calculator = lambda expr: None
        stub._picker_ctrl_enter = lambda pk, name, data: None
        stub._run_resident_feature = lambda key: None
        stub.active = types.SimpleNamespace(
            real_dir=lambda: str(here),
            show_stash=lambda: stub.stash_shown.append(True))

        def fake_open_picker(title, items, on_path, on_accept=None, **kwargs):
            captured.update(title=title, items=items, accept=on_accept,
                            delete=kwargs.get("on_delete"),
                            move=kwargs.get("on_move"),
                            placeholder=kwargs.get("placeholder"),
                            hint=kwargs.get("hint"),
                            modes=kwargs.get("modes"),
                            initial_mode=kwargs.get("initial_mode"),
                            on_mode_change=kwargs.get("on_mode_change"))
            picker.items = items
            return picker

        stub._open_picker = fake_open_picker
        filer.FilerWindow.cmd_bookmark_jump(stub)

        assert captured["placeholder"] == up.PLACEHOLDER
        assert captured["hint"] == up.HINT
        assert captured["modes"] == up.MODES
        assert captured["initial_mode"] == "["
        items = captured["items"]
        # `[` の並び: スタッシュ → フォルダの登録だけ。取り込みの行は出ない。
        assert items[0][1] == {"kind": "stash"}
        matches = [m for _n, _d, m in items]
        assert matches[1:3] == ["A", "B"], matches
        assert not any(d.get("kind") == "import" for _n, d, _m in items)

        def row(match):
            return next(e for e in picker.items if e[2] == match)

        # フォルダの行 → アクティブ窓の移動(jump_to)
        captured["accept"](*row("B")[:2])
        assert stub.jumped == [str(left)]
        # 一時置き場の行
        captured["accept"](*items[0][:2])
        assert stub.stash_shown == [True]
        # 「ここを登録」→ 名前を聞いて settings に増える
        original = launcher._ask_name
        launcher._ask_name = lambda parent, default: "C_new"
        try:
            add_row = next(e for e in picker.items
                           if isinstance(e[1], dict)
                           and e[1].get("kind") == "add")
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
        # `!` への切り替えが例外なく進む
        captured["on_mode_change"]("!")


def test_call_signatures_match():
    """filer.py / feature_screen.py から unified_picker などへ渡す引数が、
    実物の関数の引数と合っていること(キーワード名の取り違えで実行時に
    TypeError になる事故を、窓を開かずに防ぐ)。"""
    import ast
    import inspect

    import command_launcher
    import command_palette
    import launcher
    import unified_picker

    modules = {"unified_picker": unified_picker, "picker": picker_mod,
               "command_launcher": command_launcher,
               "command_palette": command_palette, "launcher": launcher}
    base = os.path.dirname(os.path.abspath(unified_picker.__file__))
    checked = 0
    for filename in ("filer.py", "feature_screen.py"):
        tree = ast.parse(open(os.path.join(base, filename),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)):
                continue
            module = modules.get(func.value.id)
            if module is None:
                continue
            target = getattr(module, func.attr, None)
            if not callable(target):
                continue
            params = inspect.signature(target).parameters
            names = list(params)
            accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                                 for p in params.values())
            positional = [p for p in params.values()
                          if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                        inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            assert len(node.args) <= len(positional), \
                (filename, func.attr, len(node.args), names)
            for kw in node.keywords:
                if kw.arg is None:  # **kwargs の展開は対象外
                    continue
                assert accepts_kwargs or kw.arg in names, \
                    (filename, func.attr, kw.arg, names)
            checked += 1
    assert checked > 20, checked


ALL_TESTS = [value for name, value in sorted(globals().items())
             if name.startswith("test_")]


def main() -> int:
    import tempfile
    from pathlib import Path

    failures = 0
    for test in ALL_TESTS:
        try:
            if "tmp_path" in test.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as tmp:
                    test(Path(tmp))
            else:
                test()
            print(f"PASS {test.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {type(e).__name__}: {e}")
    print(f"{len(ALL_TESTS) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
