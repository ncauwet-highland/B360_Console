#!/usr/bin/env python3
"""Rebuild console.ico from the Highland Technology logo.

    python tools/make_icon.py                       # writes console.ico
    python tools/make_icon.py --preview sheet.png   # plus every size side by side

The icon is the logo's mark -- three hills and the sun, no lettering -- on the
logo's own blue. The lettering is left out because at 16-48 px it is a pixel or
two tall and reads as noise.

The mark is about 5:1 wide, so it is stretched to make use of the square: the
hills by --stretch (2.5 = two and a half times their true height), the sun not
at all. It stays a true circle, only enlarged by --sun, because an oval sun stops
looking like the logo. Stretching the image itself would also stretch the line
width, so flat strokes would come out fat and steep ones thin. Instead the strokes'
centrelines are traced, stretched, and redrawn at one even weight. The weight is
chosen per icon size, so the small sizes get proportionally bolder lines.

Every size is drawn at 8x and scaled down, rather than scaling one master, so
each size is antialiased properly instead of being a blurred copy of 256.

Tied to logo.png as it is: the mark's extent (MARK) and where the sun sits (SUN)
were measured from that 249x115 file. A different logo file needs both
re-measured.

Needs Pillow (pip install pillow). The Linux build venv already has it:
~/.cache/b360_console_build/venv/bin/python tools/make_icon.py
"""

import argparse
import math
import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.stderr.write("make_icon: needs Pillow (pip install pillow)\n")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The mark's white extent in logo.png, x0, y0, x1, y1. The lettering starts at
#: y=72, so nothing below the mark is traced.
MARK = (13, 26, 223, 64)

#: Traced points in this region are the sun, x >= 185 and y <= 53. Nothing
#: belonging to a hill is up there: the last hill's tail runs below, at y >= 60.
SUN = (185, 53)

#: Sizes written into the .ico. Windows picks 16/24/32 for title bars and the
#: taskbar, 48+ for Explorer; the AppImage build takes the largest.
SIZES = (256, 128, 64, 48, 32, 24, 16)

SUPERSAMPLE = 8
MARGIN = 0.07           #< blue border, as a fraction of the icon's side
STROKE = 0.045          #< line width, as a fraction of the icon's side ...
MIN_STROKE_PX = 1.6     #< ... but never thinner than this many pixels


def trace(logo):
    """Centreline points of the mark's strokes, and the segments joining them.

    Every short white run is reduced to its brightness-weighted centre: runs down
    each column give the centreline of the shallow strokes, runs along each row
    that of the steep ones. Long runs are where a stroke lies along the scan, and
    the other scan covers them. Points within 1.6 px are then joined.
    """
    g = logo.convert("L")
    blue = g.getpixel((2, 2))
    x0, y0, x1, y1 = MARK

    def wt(x, y):
        """How white a pixel is, 0 (logo blue) to 1."""
        return max(0.0, (g.getpixel((x, y)) - blue) / (255.0 - blue))

    pts = []
    for x in range(x0 - 2, x1 + 3):
        y = y0 - 2
        while y <= y1 + 2:
            if wt(x, y) > 0.25:
                run = []
                while y <= y1 + 2 and wt(x, y) > 0.25:
                    run.append(y)
                    y += 1
                if len(run) <= 4:
                    w = sum(wt(x, v) for v in run)
                    pts.append((x, sum(v * wt(x, v) for v in run) / w))
            y += 1
    for y in range(y0 - 2, y1 + 3):
        x = x0 - 2
        while x <= x1 + 2:
            if wt(x, y) > 0.25:
                run = []
                while x <= x1 + 2 and wt(x, y) > 0.25:
                    run.append(x)
                    x += 1
                if len(run) <= 4:
                    w = sum(wt(u, y) for u in run)
                    pts.append((sum(u * wt(u, y) for u in run) / w, y))
            x += 1

    cell = {}
    for i, (x, y) in enumerate(pts):
        cell.setdefault((int(x), int(y)), []).append(i)
    edges = set()
    for i, (x, y) in enumerate(pts):
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                for j in cell.get((int(x) + dx, int(y) + dy), []):
                    if j > i and math.hypot(pts[j][0] - x, pts[j][1] - y) <= 1.6:
                        edges.add((i, j))
    return pts, sorted(edges)


class Mark:
    """The traced mark, split into hills (stretched) and the sun (a fitted circle)."""

    def __init__(self, logo):
        self.blue = logo.convert("RGB").getpixel((2, 2))
        pts, edges = trace(logo)
        sun = [p for p in pts if self.is_sun(p)]
        if not sun:
            raise ValueError("no sun found in the SUN region -- is this logo.png?")
        self.pts = pts
        self.hill_pts = [p for p in pts if not self.is_sun(p)]
        self.hill_edges = [(i, j) for i, j in edges
                           if not self.is_sun(pts[i]) and not self.is_sun(pts[j])]
        self.sun_x = sum(p[0] for p in sun) / len(sun)
        self.sun_y = sum(p[1] for p in sun) / len(sun)
        self.sun_r = sum(math.hypot(p[0] - self.sun_x, p[1] - self.sun_y)
                         for p in sun) / len(sun)

    @staticmethod
    def is_sun(p):
        return p[0] >= SUN[0] and p[1] <= SUN[1]

    def render(self, size, stretch, sun_grow):
        """One icon frame, `size` px square."""
        x0, y0, x1, y1 = MARK
        n = size * SUPERSAMPLE
        stroke = max(MIN_STROKE_PX, size * STROKE) * SUPERSAMPLE
        r = stroke / 2
        lo = MARGIN * n + r                 #< so the stroke's edge sits on the margin
        span = n - 2 * lo                   #< the mark always spans the full width
        hspan = min(span, span * stretch * (y1 - y0) / (x1 - x0))
        top = (n - hspan) / 2

        def T(p):
            return (lo + (p[0] - x0) / (x1 - x0) * span,
                    top + (p[1] - y0) / (y1 - y0) * hspan)

        img = Image.new("RGB", (n, n), self.blue)
        d = ImageDraw.Draw(img)
        for i, j in self.hill_edges:
            d.line([T(self.pts[i]), T(self.pts[j])], fill="white",
                   width=int(round(stroke)))
        for p in self.hill_pts:                         #< round joints and ends
            x, y = T(p)
            d.ellipse([x - r, y - r, x + r, y + r], fill="white")

        # The sun keeps its true proportion against the WIDTH, which is not
        # stretched, then grows by sun_grow -- held inside the margin.
        R = self.sun_r * span / (x1 - x0) * sun_grow
        cx, cy = T((self.sun_x, self.sun_y))
        edge = lo + R
        cx = min(max(cx, edge), n - edge)
        cy = min(max(cy, edge), n - edge)
        d.ellipse([cx - R - r, cy - R - r, cx + R + r, cy + R + r], fill="white")
        d.ellipse([cx - R + r, cy - R + r, cx + R - r, cy + R - r], fill=self.blue)
        return img.resize((size, size), Image.LANCZOS)


def preview(ico_path, out_path):
    """Every size as Windows will draw it, then 32/24/16 blown up pixel-exact."""
    ico = Image.open(ico_path)
    pad, zoom = 10, 64
    small = (32, 24, 16)
    sheet = Image.new("RGB", (sum(SIZES) + pad * (len(SIZES) + 1)
                              + len(small) * (zoom + pad), max(SIZES) + 2 * pad),
                      (235, 235, 235))
    x = pad
    for s in SIZES:
        sheet.paste(ico.ico.getimage((s, s)), (x, pad))
        x += s + pad
    for s in small:
        sheet.paste(ico.ico.getimage((s, s)).resize((zoom, zoom), Image.NEAREST),
                    (x, pad))
        x += zoom + pad
    sheet.save(out_path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rebuild console.ico from the Highland Technology logo.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--logo", default=os.path.join(ROOT, "logo.png"),
                    help="source logo (default logo.png at the repo root)")
    ap.add_argument("--out", default=os.path.join(ROOT, "console.ico"),
                    help="icon to write (default console.ico at the repo root)")
    ap.add_argument("--preview", help="also write a PNG showing every size")
    ap.add_argument("--stretch", type=float, default=2.5,
                    help="hill height, times true proportion (default 2.5; "
                         "about 5.4 fills the square)")
    ap.add_argument("--sun", type=float, default=1.5,
                    help="sun size, times true proportion (default 1.5)")
    args = ap.parse_args(argv)

    if args.stretch <= 0 or args.sun <= 0:
        ap.error("--stretch and --sun must be positive")
    try:
        logo = Image.open(args.logo).convert("RGBA")
        mark = Mark(logo)
    except (OSError, ValueError) as exc:
        sys.stderr.write("make_icon: %s\n" % exc)
        return 2

    frames = [mark.render(s, args.stretch, args.sun) for s in SIZES]
    frames[0].save(args.out, format="ICO", sizes=[(s, s) for s in SIZES],
                   append_images=frames[1:])

    # Read it back: what matters is what the file holds, not what was asked for.
    got = sorted(Image.open(args.out).ico.sizes())
    want = sorted((s, s) for s in SIZES)
    if got != want:
        sys.stderr.write("make_icon: %s holds %s, expected %s\n"
                         % (args.out, got, want))
        return 1
    print("wrote %s (%s px)" % (args.out, ", ".join(str(s) for s in SIZES)))

    if args.preview:
        preview(args.out, args.preview)
        print("wrote %s" % args.preview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
