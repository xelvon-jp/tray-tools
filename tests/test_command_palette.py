# test_command_palette.py
# 統合コマンドパレット(command_palette.py)のテスト。
# 検索スコア・その場計算・単位変換は Qt なしで確かめ、窓は offscreen で開く。
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import command_palette as cp


def _cand(kind, title, keywords=""):
    return cp.Candidate(kind, title, "", {}, keywords)


def test_score_match():
    assert cp.score_match("電卓", "電卓") > cp.score_match("電卓", "電卓メモ")
    assert cp.score_match("電", "電卓") is not None
    assert cp.score_match("xyz", "電卓") is None
    # 部分列(あいまい)でも拾う
    assert cp.score_match("dsk", "desktop") is not None
    # 前方一致が部分一致より上
    assert cp.score_match("cal", "calendar") > cp.score_match("cal", "recall")


def test_filter_candidates():
    items = [
        _cand("feature", "電卓", "calculator"),
        _cand("feature", "カレンダー", "calendar"),
        _cand("feature", "キャプチャ", "capture"),
    ]
    # 空の検索語は渡された順のまま
    assert cp.filter_candidates("", items) == items
    # 別名(項目キー)でも探せる
    assert [c.title for c in cp.filter_candidates("calc", items)] == ["電卓"]
    # 合わない候補は落ちる
    assert cp.filter_candidates("zzz", items) == []


def test_evaluate_arithmetic():
    def value(text):
        cand = cp.evaluate_query(text)
        return None if cand is None else cand.payload["value"]

    assert value("1+2*3") == "7"
    assert value("(1+2)*3") == "9"
    assert value("10/4") == "2.5"
    assert value("3×4") == "12"
    assert value("100÷5") == "20"
    assert value("2^10") == "1024"
    assert value("10%3") == "1"
    assert value("1,000+234") == "1234"
    # 計算でないもの・壊れた式は候補にならない
    assert cp.evaluate_query("1234") is None
    assert cp.evaluate_query("メモ") is None
    assert cp.evaluate_query("1+") is None
    assert cp.evaluate_query("1/0") is None
    assert cp.evaluate_query("__import__('os')") is None


def test_evaluate_units():
    def value(text):
        cand = cp.evaluate_query(text)
        return None if cand is None else cand.payload["value"]

    assert value("100cm → m") == "1"
    assert value("1kg → g") == "1000"
    assert value("2.5km m") == "2500"
    assert value("32f → c") == "0"
    assert value("100c → f") == "212"
    # 次元の違う変換や、同じ単位同士は候補にならない
    assert cp.evaluate_query("100cm → g") is None
    assert cp.evaluate_query("5m → m") is None


def test_scan_app_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub").mkdir()
        (root / "メモ帳.lnk").write_bytes(b"")
        (root / "sub" / "メモ帳.lnk").write_bytes(b"")   # 同名は先勝ちで1つ
        (root / "sub" / "電卓.lnk").write_bytes(b"")
        (root / "readme.txt").write_bytes(b"")
        found = cp.scan_app_candidates([str(root)])
        assert [c.title for c in found] == ["メモ帳", "電卓"]
        assert all(c.kind == "app" and c.payload["path"].endswith(".lnk") for c in found)


def test_palette_window_offscreen():
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    static = [
        _cand("feature", "電卓", "calculator"),
        _cand("feature", "カレンダー", "calendar"),
    ]
    window = cp.create_window(static)
    chosen = []
    window.chosen.connect(chosen.append)
    window.show()
    app.processEvents()
    # 空の入力では静的候補が並ぶ
    assert window._list.count() == 2
    # 打つと絞られ、先頭が選ばれる
    window._input.setText("calc")
    app.processEvents()
    assert window._list.count() == 1
    # Enter で選ばれる
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
    window._input.keyPressEvent(event)
    app.processEvents()
    assert len(chosen) == 1 and chosen[0].title == "電卓"
    window.close()
    window.deleteLater()


ALL_TESTS = [
    test_score_match,
    test_filter_candidates,
    test_evaluate_arithmetic,
    test_evaluate_units,
    test_scan_app_candidates,
    test_palette_window_offscreen,
]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
