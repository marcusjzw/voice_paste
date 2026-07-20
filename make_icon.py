#!/usr/bin/env python3
"""Generate VoicePaste.icns — a white microphone on a rounded-rect gradient.

Renders a 1024px master with AppKit, downscales to a full iconset with sips,
and packs it with iconutil. Run with the app's framework Python.
"""
import subprocess
import pathlib
import AppKit
from Foundation import NSMakeRect, NSMakeSize

REPO = pathlib.Path(__file__).parent
MASTER = 1024


def _render_master(path: pathlib.Path) -> None:
    img = AppKit.NSImage.alloc().initWithSize_(NSMakeSize(MASTER, MASTER))
    img.lockFocus()
    AppKit.NSGraphicsContext.currentContext().setImageInterpolation_(
        AppKit.NSImageInterpolationHigh)

    # Rounded-rect background with a top→bottom gradient (macOS corner ratio).
    rect = NSMakeRect(0, 0, MASTER, MASTER)
    radius = MASTER * 0.2237
    bg = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius)
    top = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.46, 0.40, 0.98, 1.0)
    bot = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.27, 0.57, 0.99, 1.0)
    AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        top, bot).drawInBezierPath_angle_(bg, -90.0)

    # White mic glyph, centered.
    glyph = None
    try:
        conf = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            MASTER * 0.5, AppKit.NSFontWeightRegular, 3)
        sym = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "mic.fill", None)
        if sym is not None:
            sym = sym.imageWithSymbolConfiguration_(conf)
            sw, sh = sym.size().width, sym.size().height
            glyph = AppKit.NSImage.alloc().initWithSize_(NSMakeSize(sw, sh))
            glyph.lockFocus()
            sym.drawAtPoint_fromRect_operation_fraction_(
                (0, 0), NSMakeRect(0, 0, sw, sh),
                AppKit.NSCompositingOperationSourceOver, 1.0)
            AppKit.NSColor.whiteColor().set()
            AppKit.NSRectFillUsingOperation(
                NSMakeRect(0, 0, sw, sh), AppKit.NSCompositingOperationSourceAtop)
            glyph.unlockFocus()
    except Exception:
        glyph = None

    if glyph is not None:
        gw, gh = glyph.size().width, glyph.size().height
        glyph.drawAtPoint_fromRect_operation_fraction_(
            ((MASTER - gw) / 2.0, (MASTER - gh) / 2.0),
            NSMakeRect(0, 0, gw, gh),
            AppKit.NSCompositingOperationSourceOver, 1.0)
    else:
        # Fallback: draw the studio-mic emoji centered.
        para = AppKit.NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(AppKit.NSTextAlignmentCenter)
        attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(MASTER * 0.55),
            AppKit.NSParagraphStyleAttributeName: para,
        }
        s = AppKit.NSString.stringWithString_("🎙")
        sz = s.sizeWithAttributes_(attrs)
        s.drawInRect_withAttributes_(
            NSMakeRect(0, (MASTER - sz.height) / 2.0, MASTER, sz.height), attrs)

    img.unlockFocus()
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    png = rep.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypePNG, {})
    png.writeToFile_atomically_(str(path), True)


def main() -> None:
    master_png = REPO / "_icon_master.png"
    _render_master(master_png)

    iconset = REPO / "VoicePaste.iconset"
    subprocess.run(["rm", "-rf", str(iconset)], check=True)
    iconset.mkdir()
    sizes = [16, 32, 128, 256, 512]
    for s in sizes:
        for scale, suffix in ((1, f"{s}x{s}"), (2, f"{s}x{s}@2x")):
            px = s * scale
            subprocess.run(
                ["sips", "-z", str(px), str(px), str(master_png),
                 "--out", str(iconset / f"icon_{suffix}.png")],
                check=True, capture_output=True)
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset),
         "-o", str(REPO / "VoicePaste.icns")], check=True)
    subprocess.run(["rm", "-rf", str(iconset)], check=True)
    master_png.unlink(missing_ok=True)
    print("Wrote", REPO / "VoicePaste.icns")


if __name__ == "__main__":
    main()
