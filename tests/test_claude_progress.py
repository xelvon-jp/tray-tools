# test_claude_progress.py
# 進捗パネル(claude_progress)の Muse 見張り行のテスト。窓は offscreen で開く。
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import claude_progress as cp


def test_muse_row():
    now = time.time()
    data = {"state": "waiting", "since": now - 30, "waiting_since": now - 30,
            "poll_sec": 15, "last_poll": now}
    row = cp.muse_row(data, now)
    assert row["_muse"] and row["state"] == "waiting"
    assert cp.row_height(row) == cp.ROW_HEIGHT
    # ファイルが無い・古すぎる場合は行にならない
    assert cp.muse_row(None, now) is None
    old = dict(data, last_poll=now - 7 * 3600)
    assert cp.muse_row(old, now) is None


def test_panel_shows_muse_row():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    panel = cp.ClaudeProgressPanel()
    now = time.time()
    data = {"state": "busy", "since": now - 65, "waiting_since": now - 252,
            "poll_sec": 15, "last_poll": now - 8, "approved": 2}
    panel.set_entries([], None, data)
    app.processEvents()
    assert [bool(r.get("_muse")) for r in panel._rows] == [True]
    assert panel.isVisible()
    # 描画まで通す(paintEvent の例外が無いこと)
    panel.grab()
    # 状態が同じ間は ✕ で消え、状態が変われば戻る
    panel._remove_row(0)
    assert panel._rows == []
    panel.set_entries([], None, data)
    assert panel._rows == []
    changed = dict(data, state="waiting", since=now)
    panel.set_entries([], None, changed)
    assert [bool(r.get("_muse")) for r in panel._rows] == [True]
    panel.close()
    panel.deleteLater()


ALL_TESTS = [test_muse_row, test_panel_shows_muse_row]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
