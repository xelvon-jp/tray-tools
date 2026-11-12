# test_taskbar_launcher.py
# タスクバーウィジェットの縦ランチャ(taskbar_launcher.py)と、カレンダーの小窓
# (calendar_popup.py)のテスト。窓の実物が要るものは offscreen で作る。
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import taskbar_launcher as launcher  # noqa: E402


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_resolve_items_calendar():
    """既定の並びにカレンダーが入り、設定の既定値にも入っている(末尾はカーソル)。"""
    assert launcher.resolve_items({}) == launcher.DEFAULT_ITEMS
    assert "calendar" in launcher.DEFAULT_ITEMS
    assert launcher.DEFAULT_ITEMS[-1] == "cursor"
    # 先頭はコマンドパレット。統合ピッカー(start_launcher)を開くようになり、
    # 呼び先が feature_screen 側に実在すること。旧ランチャ(cmdlauncher)は統合で消えた
    assert launcher.DEFAULT_ITEMS[0] == "palette"
    assert launcher.ITEMS["palette"]["method"] == "start_launcher"
    assert "cmdlauncher" not in launcher.ITEMS
    assert "cmdlauncher" not in launcher.DEFAULT_ITEMS
    assert launcher.ITEMS["cursor"]["method"] == "find_cursor"
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path("feature_screen.py").read_text(encoding="utf-8"))
    methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "start_launcher" in methods
    assert "show_palette" not in methods
    assert "find_cursor" in methods
    # 設定を書かなかった場合の受け取り方(知らない名前は飛ばす・重複は1つ)
    keys = launcher.resolve_items({"launcher_items": ["calendar", "calendar", "nope"]})
    assert keys == ["calendar"], keys
    assert launcher.resolve_items({"launcher_enabled": False}) == []

    import settings

    section = settings.DEFAULT_SETTINGS["taskbar_widget"]
    assert "calendar" in launcher.resolve_items(section)
    assert launcher.resolve_items(section)[-1] == "cursor"


def test_calendar_item_wiring():
    """項目表が画面 Feature の show_calendar を指し、グリフの絵が作れる。"""
    item = launcher.ITEMS["calendar"]
    assert item["label"] == "カレンダー"
    assert item["feature"] == "screen"
    assert item["method"] == "show_calendar"
    assert "calendar" in launcher._GLYPHS

    # 呼び先の実物があること(feature_screen は重い依存が多いので import せず構文木で見る)
    source = Path(launcher.__file__).with_name("feature_screen.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef) and cls.name == "ScreenFeature"
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "show_calendar" in methods

    _qapp()
    assert not launcher._item_pixmap("calendar").isNull()


def test_calendar_popup_behavior():
    """小窓: 今日が選ばれ、土日が色付きで、Esc と非アクティブ化で閉じる。"""
    from PySide6.QtCore import QDate, QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import calendar_popup

    _qapp()
    popup = calendar_popup.create_popup()
    fired = []
    popup.closed.connect(lambda: fired.append(True))
    popup.show_at_cursor_screen()
    assert popup.isVisible()
    cal = popup._calendar
    assert cal.selectedDate() == QDate.currentDate()
    assert cal.firstDayOfWeek() == Qt.Sunday
    assert cal.weekdayTextFormat(Qt.Saturday).foreground().color().name() == "#2563eb"
    assert cal.weekdayTextFormat(Qt.Sunday).foreground().color().name() == "#dc2626"

    QTest.keyClick(popup, Qt.Key_Escape)
    assert not popup.isVisible()
    assert fired == [True]
    popup.deleteLater()

    # フォーカスが外れたら、猶予のあとで閉じる
    popup2 = calendar_popup.create_popup()
    popup2.show_at_cursor_screen()
    assert popup2.isVisible()
    QTest.qWait(60)   # 表示に伴う有効化が落ち着いてから(実利用でのイベントの順序)
    QApplication.sendEvent(popup2, QEvent(QEvent.WindowDeactivate))
    QTest.qWait(calendar_popup.DEACTIVATE_CLOSE_MS + 120)
    assert not popup2.isVisible()
    popup2.deleteLater()


def test_toggle_item_key():
    """右クリックメニューの付け外し: 入っていれば外す、無ければ末尾に足す。"""
    keys = ["palette", "capture"]
    assert launcher.toggle_item_key(keys, "capture") == ["palette"]
    assert launcher.toggle_item_key(keys, "calendar") == ["palette", "capture", "calendar"]
    assert keys == ["palette", "capture"]   # 元のリストは変えない
    assert launcher.toggle_item_key([], "filer") == ["filer"]


def test_new_items_filer_and_bg_remove():
    """あっふ・背景を透過のボタンが用意され、呼び先が実在し、設定でも書ける。"""
    for key, method in (("filer", "start_filer"), ("bg_remove", "start_bg_remove")):
        item = launcher.ITEMS[key]
        assert item["method"] == method
        assert launcher._GLYPHS.get(item["icon"]) is not None
        assert launcher.resolve_items({"launcher_items": [key]}) == [key]
    tree = ast.parse(Path("feature_screen.py").read_text(encoding="utf-8"))
    methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "start_filer" in methods and "start_bg_remove" in methods


def test_arrangement_legacy_and_hidden():
    """並び全体と隠しリスト: 旧形式は補完で読み、新形式は hidden を落として表示する。"""
    # 旧形式(launcher_hidden が無い): 並びに入っていない項目が隠し扱い
    full, hidden = launcher.arrangement({"launcher_items": ["calendar"]})
    assert full[0] == "calendar"
    assert set(full) == set(launcher.ITEMS)
    assert "palette" in hidden and "calendar" not in hidden
    assert launcher.resolve_items({"launcher_items": ["calendar"]}) == ["calendar"]

    # 既定(何も書いていない): 従来どおり DEFAULT_ITEMS がそのまま表示される
    full, hidden = launcher.arrangement({})
    assert full[: len(launcher.DEFAULT_ITEMS)] == launcher.DEFAULT_ITEMS
    assert launcher.resolve_items({}) == launcher.DEFAULT_ITEMS

    # 新形式: launcher_items が全体の順で、hidden のものだけ落ちる。
    # どちらにも入っていない項目(新しく増えたカタログ項目)は末尾に表示される
    config = {
        "launcher_items": ["capture", "palette", "calendar"],
        "launcher_hidden": ["palette"],
    }
    resolved = launcher.resolve_items(config)
    assert resolved[:2] == ["capture", "calendar"]
    assert "palette" not in resolved
    assert set(resolved) == set(launcher.ITEMS) - {"palette"}
    full, hidden = launcher.arrangement(config)
    assert full[:3] == ["capture", "palette", "calendar"]
    assert hidden == ["palette"]
    assert resolved == [key for key in full if key not in hidden]


def test_toggle_arrangement_restores_position():
    """付け外しで位置が残る: 外して付け直すと元の場所へ戻る(末尾へ行かない)。"""
    full, hidden = launcher.toggle_arrangement(
        ["palette", "capture", "calendar"], [], "capture"
    )
    assert full == ["palette", "capture", "calendar"]   # 並びは動かない
    assert hidden == ["capture"]
    full, hidden = launcher.toggle_arrangement(full, hidden, "capture")
    assert full == ["palette", "capture", "calendar"]   # 元の位置のまま
    assert hidden == []
    # 全体に入っていない項目を付けると末尾に足す
    full, hidden = launcher.toggle_arrangement(["palette"], [], "calendar")
    assert full == ["palette", "calendar"] and hidden == []


def test_context_menu_opens_reorder_directly():
    """右クリックはメニューを挟まず、並べ替えダイアログを直接開く(#8)。"""
    _qapp()
    panel = launcher.LauncherPanel.__new__(launcher.LauncherPanel)
    opened = []
    panel._open_reorder_dialog = lambda: opened.append(True)

    class _Event:
        def globalPos(self):
            return None

    panel.contextMenuEvent(_Event())
    assert opened == [True]


def test_reorder_dialog_moves():
    """並べ替えダイアログ: 並び替えと付け外しができ、動くたびに通知される。"""
    from PySide6.QtCore import QModelIndex, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest

    _qapp()
    applied = []
    dialog = launcher.LauncherReorderDialog(
        None,
        ["palette", "capture", "calendar", "cursor"],
        ["cursor"],
        lambda full, hidden: applied.append((full, hidden)),
    )
    dialog.show()
    lst = dialog._list

    def keys():
        return [lst.item(i).data(Qt.UserRole) for i in range(lst.count())]

    assert keys() == ["palette", "capture", "calendar", "cursor"]
    # 隠している項目(cursor)はチェックなし+薄字+斜体、表示中はチェックあり
    assert lst.item(3).checkState() == Qt.Unchecked
    assert lst.item(3).foreground().color() == QColor("#9aa0a6")
    assert lst.item(3).font().italic()
    assert lst.item(0).checkState() == Qt.Checked
    assert not lst.item(0).font().italic()

    # Alt+↓ で1つ下へ。即反映の通知にも新しい並びと隠しが届く
    lst.setCurrentRow(0)
    QTest.keyClick(lst, Qt.Key_Down, Qt.AltModifier)
    assert keys() == ["capture", "palette", "calendar", "cursor"]
    assert applied[-1] == (keys(), ["cursor"])

    # 先頭で ↑ ボタンを押しても動かない(通知も増えない)
    lst.setCurrentRow(0)
    calls = len(applied)
    dialog._up_button.click()
    assert keys()[0] == "capture" and len(applied) == calls

    # ↓ ボタンでも動く
    dialog._down_button.click()
    assert keys() == ["palette", "capture", "calendar", "cursor"]
    assert applied[-1] == (keys(), ["cursor"])

    # ドラッグ相当のモデル移動でも通知される(rowsMoved)。
    # 途中で項目が欠けた並びが通知されないことも確かめる
    # (2026-11-12、アオのレビューで挙がったドラッグ中の保存の懸念)
    calls = len(applied)
    moved = lst.model().moveRow(QModelIndex(), 0, QModelIndex(), 3)
    assert moved
    assert len(applied) > calls and applied[-1][0] == keys()
    assert all(sorted(full) == ["calendar", "capture", "cursor", "palette"]
               for full, _hidden in applied[calls:])

    # チェックを外すと隠しに入り、薄字+斜体になる。付け直すと戻る
    row = keys().index("capture")
    lst.item(row).setCheckState(Qt.Unchecked)
    assert set(applied[-1][1]) == {"capture", "cursor"}
    assert lst.item(row).foreground().color() == QColor("#9aa0a6")
    assert lst.item(row).font().italic()
    lst.item(row).setCheckState(Qt.Checked)
    assert applied[-1][1] == ["cursor"]
    assert not lst.item(row).font().italic()
    dialog.close()
    dialog.deleteLater()


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