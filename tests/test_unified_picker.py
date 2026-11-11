# test_unified_picker.py
# 統合ピッカー(unified_picker.py)の候補生成・動的候補・プレビュー文と、
# picker.PickerWindow の絞り込み名(3要素タプル)対応のテスト。
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
    return {"launcher": {"bookmarks": [
        {"name": "作業", "path": tmp},
        {"name": "ページ", "path": "https://example.com/"},
        {"name": "ノート", "path": "onenote:https://example.com/p"},
        {"name": "道具", "path": r"C:\tools\app.exe", "params": "/open %param%"},
        {"name": "電卓もどき", "path": r"C:\tools\calc.exe"},
    ]}}


def _apps():
    return [cp.Candidate("app", "メモ帳", "", {"path": r"C:\apps\notepad.lnk"})]


def _dynamics(entries, **kwargs):
    args = {"entries": entries, "features": [], "apps": [],
            "web_searches": [], "file_search": None, "history_inputs": []}
    args.update(kwargs)
    return up.make_dynamic_items(**args)


# ---------------------------------------------------------------
# picker の絞り込み名
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


# ---------------------------------------------------------------
# 静的候補
# ---------------------------------------------------------------
def test_build_items():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        items = up.build_items(entries, features=[("capture", "キャプチャ")],
                               apps=_apps(), add_path=tmp)
        by_match = {match: (display, payload)
                    for display, payload, match in items}
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
        assert by_match["キャプチャ"][0] == "🔧 キャプチャ"
        assert by_match["キャプチャ"][1] == {"kind": "feature",
                                             "item_key": "capture",
                                             "name": "キャプチャ"}
        assert by_match["メモ帳"][0] == "🚀 メモ帳"
        assert by_match["メモ帳"][1]["kind"] == "app"
        builtin_names = [m for m in by_match
                         if by_match[m][1].get("kind") == "builtin"]
        assert sorted(builtin_names) == ["Command", "Config", "Reload"]
        kinds = [payload.get("kind") for _d, payload, _m in items]
        assert "add" in kinds and "import" in kinds


def test_entry_payload():
    entry = cl.Entry("郵便", "mailto:a@example.com", kind=cl.KIND_URI)
    payload = up.entry_payload(entry)
    assert payload["kind"] == "open" and payload["via"] == "uri"


# ---------------------------------------------------------------
# 履歴の解釈
# ---------------------------------------------------------------
def test_resolve_input():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        payload = up.resolve_input("道具;xyz", entries)
        assert payload["kind"] == "exec" and payload["param"] == "xyz"
        assert payload["history_text"] == "道具;xyz"
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


def test_dynamics_history():
    with tempfile.TemporaryDirectory() as tmp:
        entries = cl.build_entries(_settings(tmp))
        dynamics = _dynamics(entries, history_inputs=["道具;xyz", "知らない;1"])
        rows = dynamics("")
        assert len(rows) == 1
        assert rows[0][0].startswith("⌛")
        assert rows[0][1]["history_text"] == "道具;xyz"


def test_dynamics_fuzzy_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        settings = {"launcher": {"bookmarks": [{"name": "Google", "path": tmp}]}}
        entries = cl.build_entries(settings)
        dynamics = _dynamics(entries)
        rows = dynamics("ggl")
        assert any(p.get("kind") == "jump" for _d, p in rows)
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
    assert "もう一度" in up.preview_text(
        {"kind": "exec", "history_text": "道具;1"})
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


ALL_TESTS = [test_split_item, test_picker_match_name, test_build_items,
             test_entry_payload, test_resolve_input, test_dynamics_calc_and_web,
             test_dynamics_exec_semicolon, test_dynamics_history,
             test_dynamics_fuzzy_fallback, test_dynamics_file_search,
             test_preview_text, test_tab_text]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
