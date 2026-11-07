# test_claude_progress.py
# 進捗パネル(claude_progress)の Muse 見張り行のテスト。窓は offscreen で開く。
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import claude_progress as cp
import tempfile

_TMP = tempfile.mkdtemp(prefix="cp_test_")
_COUNTER = [0]


def _state_path():
    """テストごとの保存先(本物の claude_progress.json を読み書きしない)。"""
    _COUNTER[0] += 1
    return os.path.join(_TMP, f"state{_COUNTER[0]}.json")


def _new_panel(path=None):
    return cp.ClaudeProgressPanel(state_path=path or _state_path())


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
    panel = _new_panel()
    now = time.time()
    data = {"state": "busy", "since": now - 65, "waiting_since": now - 252,
            "poll_sec": 15, "last_poll": now - 8, "approved": 2}
    panel.set_entries([], None, data)
    app.processEvents()
    assert [bool(r.get("_muse")) for r in panel._rows] == [True]
    assert panel.isVisible()
    # 描画まで通す(paintEvent の例外が無いこと)
    panel.grab()
    # 連携ステージ行は ✕ で閉じられない
    panel._remove_row(0)
    assert [bool(r.get("_muse")) for r in panel._rows] == [True]
    changed = dict(data, state="waiting", since=now)
    panel.set_entries([], None, changed)
    assert [bool(r.get("_muse")) for r in panel._rows] == [True]
    panel.close()
    panel.deleteLater()


def test_muse_kind_row_and_dismiss_key():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    panel = _new_panel()
    now = time.time()
    data = {"kind": "approval", "kind_since": now - 100, "header": None,
            "state": "idle", "since": now - 500, "waiting_since": None,
            "poll_sec": 15, "last_poll": now - 2}
    row = cp.muse_row(data, now)
    assert row["key"] == ("approval", now - 100)
    # kind が無ければ (state, since) 基準
    assert cp.muse_row(dict(data, kind=None), now)["key"] == ("idle", now - 500)
    panel.set_entries([], None, data)
    app.processEvents()
    panel.grab()   # kind 経路の描画で例外が出ないこと
    panel._remove_row(0)   # 連携ステージ行は閉じられない
    panel.set_entries([], None, data)
    assert [bool(r.get("_muse")) for r in panel._rows] == [True]
    panel.set_entries([], None, dict(data, kind="idle", kind_since=now))
    assert [bool(r.get("_muse")) for r in panel._rows] == [True]
    panel.grab()
    panel.close()
    panel.deleteLater()


def _tone_inputs(now):
    """6つの tone それぞれになる (muse, entries)。"""
    base = {"last_poll": now - 2, "poll_sec": 15, "state": "idle", "kind": "idle",
            "kind_since": now - 300, "cards": 0, "mailbox_fail": 0}
    return {
        "work": (dict(base, kind="processing", kind_since=now - 80, header="実装中"), []),
        "claude": (base, [{"state": "running", "started": now - 125, "session_id": "a"}]),
        "approval": (dict(base, kind="approval", kind_since=now - 8, cards=1), []),
        "alert": (dict(base, last_poll=now - 200), []),
        "done": (dict(base, prev_kind="processing", kind_since=now - 2), []),
        "sleep": (base, []),
    }


def test_coop_row():
    now = time.time()
    muse, entries = _tone_inputs(now)["approval"]
    row = cp.coop_row(muse, entries, now)
    assert row["_coop"] and row["_muse"] and row["key"] == ("approval", muse["kind_since"])
    assert cp.row_height(row) == cp.coop_stage.COOP_HEIGHT
    assert 190 <= cp.row_height(row) <= 215
    assert cp.coop_row(None, entries, now) is None
    assert cp.coop_row(dict(muse, last_poll=now - 7 * 3600), entries, now) is None
    # kind が無い古いファイルは since 基準のキー
    old = {"state": "busy", "since": now - 5, "last_poll": now}
    assert cp.coop_row(old, [], now)["key"][1] == old["since"]


def test_panel_coop_all_tones_paint():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    now = time.time()
    for tone, (muse, entries) in _tone_inputs(now).items():
        panel = _new_panel()
        panel.set_entries(entries, None, muse)
        app.processEvents()
        assert panel._rows[0]["_coop"], tone
        row = panel._rows[0]
        assert cp.cv.coop_view(row["muse"], row["entries"], time.time())["tone"] == tone
        assert panel.height() >= cp.coop_stage.COOP_HEIGHT
        assert panel._timer.isActive()           # 連携ステージがある間は回す
        pm = panel.grab()                        # 描画で例外が出ないこと
        assert not pm.isNull()
        # 時刻をずらしても(アニメの各コマで)通る
        real = time.time
        try:
            for dt in (0.1, 0.37, 1.3, 2.9, 4.4):
                time.time = lambda dt=dt: real() + dt   # paintEvent は time.time() を使う
                panel.grab()
        finally:
            time.time = real
        panel.close()
        panel.deleteLater()


def test_panel_coop_cannot_be_dismissed():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    now = time.time()
    inputs = _tone_inputs(now)
    panel = _new_panel()
    muse, entries = inputs["sleep"]
    panel.set_entries(entries, None, muse)
    app.processEvents()
    panel._remove_row(0)
    assert panel._rows[0]["_coop"]               # ✕ では閉じられない
    panel._muse_dismissed = panel._rows[0]["key"]
    panel.set_entries(entries, None, muse)
    assert panel._rows[0]["_coop"]               # 古い「閉じた状態のキー」も効かない
    # 状態ファイルが無くなれば、Claude の行だけが残る(連携ステージは出ない)
    panel.set_entries(entries, None, None)
    assert [bool(r.get("_coop")) for r in panel._rows] == []
    panel.close()
    panel.deleteLater()


def _click(panel, pos):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(pos), QPointF(pos),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    panel.mousePressEvent(ev)


def test_coop_collapse_toggle_and_persist():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    now = time.time()
    muse, entries = _tone_inputs(now)["sleep"]   # Claude の行が無い(動く行は連携ステージだけ)
    path = _state_path()
    panel = _new_panel(path)
    assert panel._coop_collapsed is False        # 既定は広げた状態
    panel.set_entries(entries, None, muse)
    app.processEvents()
    coop_h = cp.coop_stage.COOP_HEIGHT
    assert cp.row_height(panel._rows[0]) == coop_h
    assert not os.path.exists(path)              # 触っていない間は書かない
    # (d) ボタン以外のクリックでは変わらない
    rect = panel._row_rect(0)
    _click(panel, rect.center())
    _click(panel, rect.topLeft() + cp.QPoint(30, 30))
    assert panel._coop_collapsed is False and not os.path.exists(path)
    # (a) ボタンのクリックでたたむ / ひろげる
    btn = cp.coop_stage.toggle_rect(rect)
    assert btn.width() == 20 and btn.height() == 20
    _click(panel, btn.center())
    assert panel._coop_collapsed is True
    assert cp.row_height(panel._rows[0]) == cp.coop_stage.COOP_COLLAPSED_HEIGHT
    assert panel.height() == cp.panel_height(panel._rows)
    assert not panel._timer.isActive()           # たたんでいる間は回さない
    # たたんだ状態でもボタンは同じ場所
    assert cp.coop_stage.toggle_rect(panel._row_rect(0)).right() == btn.right()
    _click(panel, panel._row_rect(0).center())   # ボタン以外は何もしない
    assert panel._coop_collapsed is True
    panel.grab()
    # (c) 保存され、新しいパネルで復元される
    assert cp.load_state(path).get("coop_collapsed") is True
    panel2 = _new_panel(path)
    assert panel2._coop_collapsed is True
    panel2.set_entries(entries, None, muse)
    assert cp.row_height(panel2._rows[0]) == cp.coop_stage.COOP_COLLAPSED_HEIGHT
    assert not panel2._timer.isActive()
    _click(panel2, cp.coop_stage.toggle_rect(panel2._row_rect(0)).center())
    assert panel2._coop_collapsed is False
    assert panel2._timer.isActive()
    assert cp.load_state(path).get("coop_collapsed") is False
    assert cp.row_height(panel2._rows[0]) == coop_h
    # pos など他のキーを壊さない
    cp.save_state({"pos": [5, 6]}, path)
    panel2.toggle_coop()
    assert cp.load_state(path).get("pos") == [5, 6]
    for pnl in (panel, panel2):
        pnl.close()
        pnl.deleteLater()


def test_coop_collapsed_all_tones_paint():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    now = time.time()
    for tone, (muse, entries) in _tone_inputs(now).items():
        path = _state_path()
        cp.save_state({"coop_collapsed": True}, path)
        panel = _new_panel(path)
        panel.set_entries(entries, None, muse)
        app.processEvents()
        row = panel._rows[0]
        assert row["_coop"] and row["_collapsed"], tone
        assert cp.cv.coop_view(row["muse"], row["entries"], time.time())["tone"] == tone
        assert not panel.grab().isNull()
        # tone が変わっても(たたんだまま)色線とタイトルが更新される
        panel.set_entries(entries, None, _tone_inputs(now)["sleep"][0])
        panel.grab()
        panel.close()
        panel.deleteLater()


ALL_TESTS = [test_muse_row, test_panel_shows_muse_row,
             test_muse_kind_row_and_dismiss_key, test_coop_row,
             test_panel_coop_all_tones_paint, test_panel_coop_cannot_be_dismissed,
             test_coop_collapse_toggle_and_persist, test_coop_collapsed_all_tones_paint]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
