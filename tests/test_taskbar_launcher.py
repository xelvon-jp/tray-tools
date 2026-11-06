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
    """既定の並びにカレンダーが入り、設定の既定値にも入っている(末尾は電卓)。"""
    assert launcher.resolve_items({}) == launcher.DEFAULT_ITEMS
    assert "calendar" in launcher.DEFAULT_ITEMS
    assert launcher.DEFAULT_ITEMS[-1] == "calculator"
    # 先頭はコマンドパレット。呼び先が feature_screen 側に実在すること
    assert launcher.DEFAULT_ITEMS[0] == "palette"
    assert launcher.ITEMS["palette"]["method"] == "show_palette"
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path("feature_screen.py").read_text(encoding="utf-8"))
    methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "show_palette" in methods
    # 設定を書かなかった場合の受け取り方(知らない名前は飛ばす・重複は1つ)
    keys = launcher.resolve_items({"launcher_items": ["calendar", "calendar", "nope"]})
    assert keys == ["calendar"], keys
    assert launcher.resolve_items({"launcher_enabled": False}) == []

    import settings

    section = settings.DEFAULT_SETTINGS["taskbar_widget"]
    assert "calendar" in launcher.resolve_items(section)
    assert launcher.resolve_items(section)[-1] == "calculator"


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
