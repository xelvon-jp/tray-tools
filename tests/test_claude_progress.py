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
    # 履歴テープ(4行)ぶん増え、最終イメージの場面でステージが 148 に
    # 広がったので上限は 320(2026-11-10)
    assert 190 <= cp.row_height(row) <= 320
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


def test_item_position():
    ip = cp.coop_stage.item_position
    w = 274
    right = w - 100
    # fly: 手紙は 0.85 で右端に着き、そこから消える。受領は1.5秒遅れて逆向き
    assert ip("fly", "letter", 0, w) == (72, 1.0)
    x, a = ip("fly", "letter", 3000 * 0.85, w)
    assert abs(x - right) < 1e-6 and a == 1.0
    x, a = ip("fly", "letter", 3000 * 0.925, w)
    assert x == right and abs(a - 0.5) < 1e-6
    assert ip("fly", "letter", 3000, w) == (72, 1.0)       # 周期 3000
    assert ip("fly", "ack", 1500, w) == (right, 1.0)       # 1.5秒ずれて右端から出発
    x, a = ip("fly", "ack", 1500 + 3000 * 0.85, w)
    assert abs(x - 72) < 1e-6 and a == 1.0
    assert ip("fly", "ack", 0, w)[0] != ip("fly", "letter", 0, w)[0]
    # back: 受領だけが右から左へ(周期 2400)
    assert ip("back", "ack", 0, w) == (right, 1.0)
    x, a = ip("back", "ack", 2400 * 0.85, w)
    assert abs(x - 72) < 1e-6 and a == 1.0
    assert ip("back", "letter", 0, w) is None
    # parked / wobble は手紙だけ
    assert ip("parked", "letter", 123, w) == (right, 1.0) and ip("parked", "ack", 0, w) is None
    xs = {ip("wobble", "letter", t, w)[0] for t in (0, 100, 200, 300)}
    assert xs == {w * 0.46 - 2, w * 0.46 + 2}
    assert ip("wobble", "ack", 0, w) is None
    # どの時刻でも x は範囲内、不透明度は 0〜1
    for kind in ("fly", "back", "parked", "wobble"):
        for item in ("letter", "ack"):
            for t in range(0, 7000, 37):
                pos = ip(kind, item, t, w)
                if pos is not None:
                    assert 60 <= pos[0] <= w and 0.0 <= pos[1] <= 1.0


def test_rows_to_show_prunes_first_seen():
    now = time.time()
    seen = {"gone": now - 100, "a": now - 1}
    entries = [{"session_id": "a", "state": "done"}, {"session_id": "b", "state": "done"}]
    rows = cp.rows_to_show(entries, now, seen)
    assert set(seen) == {"a", "b"}                 # 一覧に無い gone は落ちる
    assert [r["_compact"] for r in rows] == [False, False]
    # キーが無い entry は控えを作らない(折りたたんだ行になる)
    rows = cp.rows_to_show([{"state": "done"}], now, seen)
    assert seen == {} and rows[0]["_compact"] is True


def test_paint_survives_row_exception():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    now = time.time()
    muse, entries = _tone_inputs(now)["claude"]
    panel = _new_panel()
    panel.set_entries(entries, None, muse)
    app.processEvents()
    real = cp.coop_stage.paint_coop

    def boom(*a, **k):
        raise RuntimeError("boom")

    cp.coop_stage.paint_coop = boom
    try:
        assert not panel.grab().isNull()           # 例外は握らず traceback を出すだけで落ちない
    finally:
        cp.coop_stage.paint_coop = real
    panel.close()
    panel.deleteLater()


def test_pulse_alpha():
    stage = cp.coop_stage
    values = [stage.pulse_alpha(t) for t in range(0, stage.PULSE_PERIOD_MS, 100)]
    assert min(values) >= stage.PULSE_MIN and max(values) <= stage.PULSE_MAX
    assert stage.pulse_alpha(0) == stage.PULSE_MIN
    assert stage.pulse_alpha(stage.PULSE_PERIOD_MS) == stage.PULSE_MIN   # 周期で戻る
    assert stage.pulse_alpha(stage.PULSE_PERIOD_MS // 2) == stage.PULSE_MAX


def test_pause_rect_left_of_toggle():
    from PySide6.QtCore import QRect
    rect = QRect(0, 0, 300, cp.coop_stage.COOP_HEIGHT)
    pause = cp.coop_stage.pause_rect(rect)
    toggle = cp.coop_stage.toggle_rect(rect)
    assert pause.width() == toggle.width() == 20 and pause.top() == toggle.top()
    assert pause.right() < toggle.left()          # 重ならない・左隣
    collapsed = QRect(0, 0, 300, cp.coop_stage.COOP_COLLAPSED_HEIGHT)
    assert cp.coop_stage.pause_rect(collapsed).top() == \
        cp.coop_stage.toggle_rect(collapsed).top()   # たたんだ状態でも同じ段


def test_panel_watch_pause_toggle():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    now = time.time()
    muse, entries = _tone_inputs(now)["sleep"]
    flag = Path(_TMP) / f"pause{_COUNTER[0]}.json"
    old = cp.bctl.PAUSE_PATH
    cp.bctl.PAUSE_PATH = flag
    try:
        panel = _new_panel()
        panel.set_entries(entries, None, muse)
        app.processEvents()
        assert panel._rows[0]["paused"] is False
        panel._toggle_watch_pause(0)              # 休止の合図を書く
        assert cp.bctl.is_paused() is True
        panel.set_entries(entries, None, muse)
        app.processEvents()
        assert panel._rows[0]["paused"] is True   # 次の受け取りで表示も休止に
        panel._toggle_watch_pause(0)              # 再開
        assert cp.bctl.is_paused() is False
        panel.close()
        panel.deleteLater()
    finally:
        cp.bctl.PAUSE_PATH = old


def test_show_bridge_expands():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    now = time.time()
    muse, entries = _tone_inputs(now)["sleep"]
    path = _state_path()
    cp.save_state({"coop_collapsed": True}, path)
    panel = _new_panel(path)
    assert panel._coop_collapsed is True
    panel.set_entries(entries, None, muse)
    app.processEvents()
    assert cp.row_height(panel._rows[0]) == cp.coop_stage.COOP_COLLAPSED_HEIGHT
    panel.show_bridge()
    app.processEvents()
    assert panel._coop_collapsed is False
    assert cp.row_height(panel._rows[0]) == cp.coop_stage.COOP_HEIGHT
    assert cp.load_state(path)["coop_collapsed"] is False   # 覚えた状態も戻る
    panel.close()
    panel.deleteLater()


def test_bridge_size_state():
    """大きさの保存と読み戻し。知らない値は large に倒す。"""
    path = _state_path()
    assert cp.load_bridge_size(path) == "large"
    cp.save_state({"bridge_size": "stealth"}, path)
    assert cp.load_bridge_size(path) == "stealth"
    cp.save_state({"bridge_size": "huge"}, path)
    assert cp.load_bridge_size(path) == "large"


def test_coop_height_sizes():
    """大きさごとの行の高さ: large が現状(COOP_HEIGHT)と一致し、中・小は低い。"""
    stage = cp.coop_stage
    assert stage.coop_height("large") == stage.COOP_HEIGHT
    assert stage.coop_height("medium") < stage.coop_height("large")
    assert stage.coop_height("small") < stage.coop_height("medium")
    assert stage.coop_height("nonsense") == stage.COOP_HEIGHT
    row = {"_coop": True, "_size": "small"}
    assert cp.row_height(row) == stage.coop_height("small")
    row["_collapsed"] = True
    assert cp.row_height(row) == stage.COOP_COLLAPSED_HEIGHT


def test_panel_stealth_and_sizes():
    """stealth では帯だけ(幅 STEALTH_W)になり、広げる条件で large の姿に戻る。
    medium では行の高さが medium のものになる。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    path = _state_path()
    cp.save_state({"bridge_size": "stealth"}, path)
    panel = _new_panel(path)
    now = time.time()
    muse, entries = _tone_inputs(now)["approval"]
    panel.set_entries(entries, None, muse)
    app.processEvents()
    assert panel._stealth_mode
    assert panel.width() == cp.coop_stage.STEALTH_W
    assert panel.isVisible()
    # 状態変化で広げる: until を未来に置くと large の姿になる
    panel._stealth_until = time.time() + 60
    panel._tick()
    app.processEvents()
    assert not panel._stealth_mode
    assert panel.width() == cp.PANEL_WIDTH
    assert panel._rows[0]["_size"] == "large"
    # medium に切り替えると保存され、行の高さも medium になる
    panel.set_bridge_size("medium")
    app.processEvents()
    assert cp.load_bridge_size(path) == "medium"
    assert panel._rows[0]["_size"] == "medium"
    assert panel.height() == cp.panel_height(panel._rows)
    panel.hide()


ALL_TESTS = [test_muse_row, test_panel_shows_muse_row,
             test_muse_kind_row_and_dismiss_key, test_coop_row,
             test_panel_coop_all_tones_paint, test_panel_coop_cannot_be_dismissed,
             test_coop_collapse_toggle_and_persist, test_coop_collapsed_all_tones_paint,
             test_item_position, test_rows_to_show_prunes_first_seen,
             test_paint_survives_row_exception, test_pulse_alpha,
             test_pause_rect_left_of_toggle, test_panel_watch_pause_toggle,
             test_show_bridge_expands, test_bridge_size_state,
             test_coop_height_sizes, test_panel_stealth_and_sizes]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
