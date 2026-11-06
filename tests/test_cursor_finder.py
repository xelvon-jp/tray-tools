# test_cursor_finder.py
# カーソルファインダー(cursor_finder.py)のテスト。波紋の計算は純関数、窓は offscreen。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cursor_finder as cf


def test_ripple_state():
    states = cf.ripple_state(0)
    assert len(states) == 1                       # 最初は1つ目の輪だけ
    radius, alpha = states[0]
    assert radius == cf.MIN_RADIUS and alpha == 1.0
    # 途中: 輪が増え、前の輪ほど大きく薄い
    states = cf.ripple_state(400)
    assert len(states) == cf.RING_COUNT
    radii = [r for r, _a in states]
    alphas = [a for _r, a in states]
    assert radii == sorted(radii, reverse=True)
    assert alphas == sorted(alphas)
    assert all(0 < a <= 1 for a in alphas)
    # 終わり際は全部消えている
    assert cf.ripple_state(cf.DURATION_MS) == []
    assert cf.ripple_state(cf.DURATION_MS + 500) == []
    # 中心の点はだんだん薄くなる
    assert cf.fade_alpha(0) == 1.0
    assert 0 < cf.fade_alpha(cf.DURATION_MS // 2) < 1
    assert cf.fade_alpha(cf.DURATION_MS) == 0.0


def test_overlay_offscreen():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    overlay = cf.CursorFinderOverlay()
    overlay.start()
    app.processEvents()
    assert overlay.isVisible()
    assert overlay._timer.isActive()
    overlay._tick()                               # 途中経過では隠れない
    assert overlay.isVisible()
    # 時間を過ぎたことにして tick すると止まって隠れる
    overlay._clock = _FakeClock(cf.DURATION_MS + 1)
    overlay._tick()
    assert not overlay.isVisible()
    assert not overlay._timer.isActive()
    overlay.deleteLater()


class _FakeClock:
    def __init__(self, elapsed):
        self._elapsed = elapsed

    def elapsed(self):
        return self._elapsed

    def isValid(self):  # noqa: N802 (Qt の名前)
        return True


ALL_TESTS = [test_ripple_state, test_overlay_offscreen]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
