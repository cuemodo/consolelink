"""Draw the menu-bar app's icons into src/consolelink/menubar_icons/ (macOS, needs PyObjC, which
`pip install 'consolelink[macos]'` brings along). Run it again after changing the drawing.

The drawing is the favicon's (three faders with a knob each) as a macOS "template" image: black on
transparent, so the menu bar tints it for light and dark mode. A template can't carry colours, so
the favicon's dark tile and its blue and warm knobs are gone; "waiting" is the same drawing
faded, "connected" is full strength.
"""
from pathlib import Path

from AppKit import (NSBezierPath, NSBitmapImageRep, NSColor, NSGraphicsContext,
                    NSPNGFileType)

OUT = Path(__file__).resolve().parent.parent / "src" / "consolelink" / "menubar_icons"
PIXELS = 40  # drawn for a 20 pt menu-bar slot at 2x (Retina)
SCALE = PIXELS / 32  # laid out on a 32 x 32 grid; favicon.svg is the same layout on 256 x 256

# From favicon.svg, divided by 8: fader tracks (x, y, w, h) and knobs (x, y, w, h), y measured from
# the top.
TRACKS = [(7, 6, 2, 20), (15, 6, 2, 20), (23, 6, 2, 20)]
KNOBS = [(4.5, 16, 7, 4), (12.5, 9, 7, 4), (20.5, 20, 7, 4)]


def draw(path, strength):
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, PIXELS, PIXELS, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))

    def rounded(x, y, w, h, alpha):
        # SVG y runs down, Cocoa's up
        rect = ((x * SCALE, (32 - y - h) * SCALE), (w * SCALE, h * SCALE))
        radius = min(w, h) * SCALE / 2 * (0.75 if w > h else 1)
        NSColor.colorWithCalibratedWhite_alpha_(0, alpha * strength).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()

    for x, y, w, h in TRACKS:
        rounded(x - 0.5, y, w + 1, h, 0.55)  # a touch thicker than the favicon: legible at 20 pt
    for x, y, w, h in KNOBS:
        rounded(x, y, w, h, 1.0)
    NSGraphicsContext.restoreGraphicsState()
    path.write_bytes(bytes(rep.representationUsingType_properties_(NSPNGFileType, None)))
    print("wrote", path)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    draw(OUT / "connected.png", 1.0)
    draw(OUT / "waiting.png", 0.4)
