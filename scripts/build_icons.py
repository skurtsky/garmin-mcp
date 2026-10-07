"""Render the Ridgeline icon set in static/icons/ from its SVG sources.

Dev-only (needs cairosvg and Pillow from requirements-dev.txt):

    python scripts/build_icons.py
"""
import io
import shutil
from pathlib import Path

import cairosvg
from PIL import Image

ICONS = Path(__file__).resolve().parent.parent / "static" / "icons"
FULLBLEED = ICONS / "ridgeline-fullbleed.svg"
FAVICON = ICONS / "ridgeline-favicon.svg"


def render(svg: Path, size: int, background: str | None = None) -> Image.Image:
    png = cairosvg.svg2png(url=str(svg), output_width=size, output_height=size,
                           background_color=background)
    return Image.open(io.BytesIO(png))


def main() -> None:
    for name, size in (("icon-512.png", 512), ("icon-192.png", 192)):
        render(FULLBLEED, size).save(ICONS / name, optimize=True)
    # iOS applies its own corner mask and rejects transparency: opaque RGB.
    render(FULLBLEED, 180).convert("RGB").save(ICONS / "apple-touch-icon.png", optimize=True)

    # ICO has no colour-scheme switch, so ship one per scheme (see ICON_LINKS).
    # Light tab bars: dark stroke. Dark tab bars: light stroke.
    for name, stroke in (("favicon.ico", "#161826"), ("favicon-dark.ico", "#E8EEFF")):
        svg = FAVICON.read_text().replace("stroke: #161826", f"stroke: {stroke}") \
                                  .replace("@media (prefers-color-scheme: dark) { .line { stroke: #E8EEFF; } }", "")
        png = cairosvg.svg2png(bytestring=svg.encode(), output_width=64, output_height=64)
        Image.open(io.BytesIO(png)).save(ICONS / name, format="ICO", sizes=[(16, 16), (32, 32)])
    shutil.copyfile(FAVICON, ICONS / "favicon.svg")


if __name__ == "__main__":
    main()
