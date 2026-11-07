import sys

import pytest

from landrop import tray

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_icon_image_sizes():
    assert tray.icon_image().size == (64, 64)
    assert tray.icon_image(16).getpixel((8, 8))[3] == 255  # 縮小しても中身がある


def test_startup_shortcut_roundtrip(tmp_path):
    lnk = tmp_path / "LAN Drop.lnk"
    assert not tray.startup_enabled(lnk)
    tray.set_startup(True, lnk)
    assert tray.startup_enabled(lnk) and lnk.stat().st_size > 0
    tray.set_startup(False, lnk)
    assert not tray.startup_enabled(lnk)


def test_port_in_use_detects_listener():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert tray.port_in_use(s.getsockname()[1])
