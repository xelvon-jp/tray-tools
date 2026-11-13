# editor_icon のテスト。アイコンが描けて、サイズごとに中身が入ること。
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtWidgets import QApplication  # noqa: E402

from editor import editor_icon  # noqa: E402

_app = QApplication.instance() or QApplication([])


def test_render_sizes_have_content():
    for size in (16, 24, 32, 48, 64):
        pixmap = editor_icon.render(size)
        assert pixmap.width() == size and pixmap.height() == size
        image = pixmap.toImage()
        # 透明でない画素がある(何か描かれている)こと。
        assert not image.isNull()
        has_opaque = any(
            image.pixelColor(x, size // 2).alpha() > 0
            for x in range(size))
        assert has_opaque


def test_qicon_has_sizes():
    icon = editor_icon.qicon()
    sizes = {s.width() for s in icon.availableSizes()}
    assert {16, 24, 32, 48, 64} <= sizes


if __name__ == "__main__":
    test_render_sizes_have_content()
    test_qicon_has_sizes()
    print("2/2 passed")
