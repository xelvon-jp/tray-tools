# tests/test_capture_measure.py
# 付箋(CaptureWindow)のスポイトと定規を確かめる。ズームと高DPIを掛けた状態で、
# 色・距離が元画像の画素で出ること、Ctrl+Z で消えること、保存・コピーへの出し分け、
# 待機役の使い回し(prepare_for_capture)で前の1枚の状態が残らないことを見る。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_capture_measure.py
#
# 環境変数 MEASURE_GRAB_DIR を渡すと、見た目の確認用に QWidget.grab() の PNG をそこへ書く。
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_screen_mirror.py と同じ形)。
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import capture_window  # noqa: E402
import sticky_measure  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

# トーストとクリップボードは画面・終了処理に絡む(offscreen の setMimeData は終了時に落ちる)。
capture_window.show_toast = lambda *a, **k: None
COPIED = []

DPR = 1.5
W_PX, H_PX = 300, 180


def make_image() -> QImage:
    """左半分 #1E90FF、右半分 #FFA500、(100,50) の1画素だけ #00FF00 の絵(画素 300x180, dpr 1.5)。"""
    image = QImage(W_PX, H_PX, QImage.Format_RGB32)
    image.fill(QColor("#1E90FF"))
    painter = QPainter(image)
    painter.fillRect(W_PX // 2, 0, W_PX // 2, H_PX, QColor("#FFA500"))
    painter.end()
    image.setPixelColor(100, 50, QColor("#00FF00"))
    image.setDevicePixelRatio(DPR)
    return image


def make_window():
    window = capture_window.CaptureWindow(make_image(), QPoint(0, 0), {})
    window._copy_color_text = lambda hex_color: COPIED.append(hex_color)
    return window


def mouse(window, kind, widget_pos, modifiers=Qt.NoModifier, buttons=Qt.LeftButton):
    pos = QPointF(*widget_pos)
    button = Qt.NoButton if kind == QEvent.MouseMove else Qt.LeftButton
    event = QMouseEvent(kind, pos, pos, button, buttons, modifiers)
    {QEvent.MouseButtonPress: window.mousePressEvent,
     QEvent.MouseMove: window.mouseMoveEvent,
     QEvent.MouseButtonRelease: window.mouseReleaseEvent}[kind](event)


def widget_pos_of_pixel(window, px, py):
    """画素(px,py)の真ん中が、ズーム後のウィジェット上のどこに来るか。"""
    return ((px + 0.5) / DPR * window.zoom_factor, (py + 0.5) / DPR * window.zoom_factor)


def click(window, widget_pos, modifiers=Qt.NoModifier):
    mouse(window, QEvent.MouseButtonPress, widget_pos, modifiers)
    mouse(window, QEvent.MouseButtonRelease, widget_pos, modifiers)


def drag(window, start, end, modifiers=Qt.NoModifier):
    mouse(window, QEvent.MouseButtonPress, start, modifiers)
    mouse(window, QEvent.MouseMove, end, modifiers)
    mouse(window, QEvent.MouseButtonRelease, end, modifiers)


# --------------------------------------------------------------------------

def test_pure_helpers():
    assert sticky_measure.ruler_metrics((10, 5), (10, 5))[:2] == (1, 1)
    w, h, d = sticky_measure.ruler_metrics((0, 0), (99, 49))
    assert (w, h) == (100, 50) and abs(d - 111.80) < 0.01
    assert sticky_measure.ruler_text((100, 1, 100.0)) == "100 px"
    assert sticky_measure.ruler_text((1, 40, 40.0)) == "40 px"
    assert sticky_measure.ruler_text((100, 50, 111.803)) == "100 × 50 / 対角線 111.8 px"
    assert sticky_measure.color_text(QColor("#1e90ff")) == "#1E90FF  RGB(30,144,255)"
    assert sticky_measure.constrain_axis((10, 10), (50, 20)) == (50, 10)
    assert sticky_measure.constrain_axis((10, 10), (12, 40)) == (10, 40)
    # 画像の外は端の画素に留める
    px, py, _ = sticky_measure.snap_to_pixel(QPointF(-5, 999), DPR, W_PX, H_PX)
    assert (px, py) == (0, H_PX - 1)


def test_eyedropper_uses_source_pixel_under_zoom_and_dpr():
    COPIED.clear()
    window = make_window()
    window._apply_zoom(2.0)
    window._begin_measure_mode("color")
    click(window, widget_pos_of_pixel(window, 100, 50))
    assert len(window.actions_history) == 1
    action = window.actions_history[0]
    assert action["pixel"] == (100, 50), action["pixel"]
    assert action["color"].name().upper() == "#00FF00"
    assert COPIED == ["#00FF00"]
    # 隣の画素は元の地の色(ズームで1画素が3px四方に見えているので、取り違えやすい)
    click(window, widget_pos_of_pixel(window, 101, 50))
    assert window.actions_history[-1]["color"].name().upper() == "#1E90FF"
    window.undo()
    window.undo()
    assert window.actions_history == []
    window.close()


def test_ruler_measures_in_source_pixels():
    window = make_window()
    window._apply_zoom(0.5)
    window._begin_measure_mode("ruler")
    drag(window, widget_pos_of_pixel(window, 10, 20), widget_pos_of_pixel(window, 109, 69))
    action = window.actions_history[-1]
    assert action["start_px"] == (10, 20) and action["end_px"] == (109, 69), action
    assert action["metrics"][:2] == (100, 50)
    # Shift で水平に揃う
    drag(window, widget_pos_of_pixel(window, 10, 20), widget_pos_of_pixel(window, 200, 30),
         Qt.ShiftModifier)
    assert window.actions_history[-1]["end_px"] == (200, 20)
    assert sticky_measure.ruler_text(window.actions_history[-1]["metrics"]) == "191 px"
    # その場のクリックは残さない
    click(window, widget_pos_of_pixel(window, 30, 30))
    assert len(window.actions_history) == 2
    window.close()


def test_ctrl_drag_still_draws_pen_in_measure_mode():
    window = make_window()
    window._begin_measure_mode("ruler")
    drag(window, (10, 10), (60, 40), Qt.ControlModifier)
    assert [a["type"] for a in window.actions_history] == ["stroke"]
    window.close()


def test_output_toggle():
    window = make_window()
    window._begin_measure_mode("color")
    click(window, widget_pos_of_pixel(window, 100, 50))
    window._end_measure_mode()
    drag(window, (10, 10), (60, 40), Qt.ControlModifier)   # ペンの線も1本
    assert len(window._export_actions()) == 2
    window.measure_in_output = False
    exported = window._export_actions()
    assert [a["type"] for a in exported] == ["stroke"]
    assert len(window.actions_history) == 2           # 画面上には残っている
    with_m = capture_window.render_annotated(window.base_image, window.actions_history)
    without = capture_window.render_annotated(window.base_image, exported)
    assert with_m != without
    assert with_m.size() == window.base_image.size()  # 元の画素数のまま焼き込む
    window.close()


def test_prepare_for_capture_resets_measure_state():
    window = make_window()
    window._begin_measure_mode("ruler")
    mouse(window, QEvent.MouseButtonPress, (20, 20))
    window.measure_in_output = False
    window.prepare_for_capture(make_image(), QPoint(0, 0))
    assert window._measure_mode is None
    assert window._ruler_start is None and window._measure_hover is None
    assert window.measure_in_output is True
    assert window.actions_history == []
    # トップレベル窓では unsetCursor() 後も WA_SetCursor が残る(Qt の仕様)ので形で見る
    assert window.cursor().shape() != Qt.CrossCursor
    window.close()


def test_grab_for_visual_check():
    out = os.environ.get("MEASURE_GRAB_DIR")
    window = make_window()
    window._begin_measure_mode("color")
    click(window, widget_pos_of_pixel(window, 100, 50))
    click(window, widget_pos_of_pixel(window, 250, 150))
    window._begin_measure_mode("ruler")
    drag(window, widget_pos_of_pixel(window, 10, 100), widget_pos_of_pixel(window, 140, 170))
    drag(window, widget_pos_of_pixel(window, 160, 20), widget_pos_of_pixel(window, 290, 25),
         Qt.ShiftModifier)
    # 定規を引いている途中(プレビュー)
    mouse(window, QEvent.MouseButtonPress, widget_pos_of_pixel(window, 20, 20))
    mouse(window, QEvent.MouseMove, widget_pos_of_pixel(window, 80, 60))
    shots = {"zoom100": window.grab()}
    window._apply_zoom(2.0)
    shots["zoom200"] = window.grab()
    for shot in shots.values():
        assert not shot.isNull()
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        for name, shot in shots.items():
            shot.save(str(Path(out) / f"measure_{name}.png"))
        capture_window.render_annotated(window.base_image, window.actions_history).save(
            str(Path(out) / "measure_baked.png"))
    window.close()


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
