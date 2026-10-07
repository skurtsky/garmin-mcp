import os
import re
import struct

import server

ICONS = os.path.join(os.path.dirname(__file__), "..", "static", "icons")


def _png_size(name):
    with open(os.path.join(ICONS, name), "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def test_png_icons_have_expected_sizes():
    assert _png_size("icon-192.png") == (192, 192)
    assert _png_size("icon-512.png") == (512, 512)
    assert _png_size("apple-touch-icon.png") == (180, 180)


def test_favicon_ico_has_16_and_32_frames():
    with open(os.path.join(ICONS, "favicon.ico"), "rb") as f:
        data = f.read()
    count = struct.unpack("<H", data[4:6])[0]
    frames = {(data[6 + 16 * i] or 256, data[7 + 16 * i] or 256) for i in range(count)}
    assert {(16, 16), (32, 32)} <= frames


def test_favicon_svg_is_pure_vector():
    with open(os.path.join(ICONS, "favicon.svg")) as f:
        svg = f.read()
    assert "<image" not in svg
    assert "data:" not in svg


def test_every_served_icon_file_exists():
    src = open(server.__file__).read()
    names = re.findall(r'"/[^"]*": \("([^"]+\.(?:png|ico|svg))"', src)
    assert names
    for name in names:
        assert os.path.isfile(os.path.join(ICONS, name)), name
