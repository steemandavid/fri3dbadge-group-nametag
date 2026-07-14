#!/usr/bin/env python3
"""Generate 10 logo proposals for the !Fri3d Friends app.

Each is a 360x360 PNG on a rounded app-tile, designed to stay legible when
downscaled to the badge splash (~120px) / launcher icon (64px). Also writes a
contact-sheet grid + an index.html gallery for review.

Run: python3 tools/make_logos.py   (output in tools/logo_proposals/)
"""
import os
import math
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "logo_proposals")
os.makedirs(OUT, exist_ok=True)
S = 360                       # canvas
R = 64                        # tile corner radius
SS = 4                        # supersample factor for crisp edges

BLACK = "/tmp/Montserrat-Black.ttf"
BOLD = "/tmp/Montserrat-Bold.ttf"

# Fri3d-ish palette
NAVY = (20, 24, 38)
NAVY2 = (14, 17, 27)
WHITE = (245, 247, 250)
YELLOW = (255, 214, 77)
TEAL = (34, 211, 197)
PINK = (255, 92, 122)
BLUE = (127, 178, 255)
GREEN = (159, 224, 160)


def font(path, size):
    return ImageFont.truetype(path, size)


def new_tile(bg):
    """Return (img, draw) at supersampled size with a rounded tile filled bg."""
    img = Image.new("RGBA", (S * SS, S * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S * SS - 1, S * SS - 1], radius=R * SS, fill=bg)
    return img, d


def finish(img, name):
    img = img.resize((S, S), Image.LANCZOS)
    img.save(os.path.join(OUT, name))
    return name


def ctext(d, xy, text, fnt, fill, anchor="mm"):
    d.text((xy[0] * SS, xy[1] * SS), text, font=font(fnt, fnt_size), fill=fill, anchor=anchor)


def T(size, path=BLACK):
    return font(path, size * SS)


def center_text(d, cx, cy, text, f, fill, anchor="mm"):
    d.text((cx * SS, cy * SS), text, font=f, fill=fill, anchor=anchor)


# ---------------------------------------------------------------------------
def logo01_signal_heart():
    """Proximity signal arcs + a heart at the source."""
    img, d = new_tile(NAVY)
    cx, cy = 180, 205
    # concentric signal arcs from lower-left
    for i, col in enumerate((BLUE, TEAL, YELLOW)):
        rr = (60 + i * 46) * SS
        d.arc([cx * SS - rr, cy * SS - rr, cx * SS + rr, cy * SS + rr],
              start=290, end=340, fill=col, width=14 * SS)
    # heart
    hx, hy, hs = 150, 150, 30
    for (ox) in (-1, 1):
        d.ellipse([(hx + ox * hs / 2 - hs / 2) * SS, (hy - hs / 2) * SS,
                   (hx + ox * hs / 2 + hs / 2) * SS, (hy + hs / 2) * SS], fill=PINK)
    d.polygon([((hx - hs) * SS, (hy) * SS), ((hx + hs) * SS, hy * SS),
               (hx * SS, (hy + hs * 1.3) * SS)], fill=PINK)
    center_text(d, 180, 300, "Fri3d Friends", T(30, BOLD), WHITE)
    return finish(img, "01_signal_heart.png")


def logo02_chat_bubbles():
    """Two overlapping speech bubbles = friends chatting."""
    img, d = new_tile(NAVY)
    def bubble(x, y, w, h, col, tail):
        d.rounded_rectangle([x * SS, y * SS, (x + w) * SS, (y + h) * SS], radius=28 * SS, fill=col)
        if tail == "l":
            d.polygon([((x + 20) * SS, (y + h) * SS), ((x + 55) * SS, (y + h) * SS),
                       ((x + 15) * SS, (y + h + 34) * SS)], fill=col)
        else:
            d.polygon([((x + w - 20) * SS, (y + h) * SS), ((x + w - 55) * SS, (y + h) * SS),
                       ((x + w - 15) * SS, (y + h + 34) * SS)], fill=col)
    bubble(60, 96, 150, 100, TEAL, "l")
    bubble(150, 150, 150, 100, YELLOW, "r")
    # dots in the front bubble
    for i in range(3):
        d.ellipse([(180 + i * 30) * SS - 8 * SS, 200 * SS - 8 * SS,
                   (180 + i * 30) * SS + 8 * SS, 200 * SS + 8 * SS], fill=NAVY)
    center_text(d, 180, 315, "!Fri3d Friends", T(26, BOLD), WHITE)
    return finish(img, "02_chat_bubbles.png")


def logo03_f3_monogram():
    """Bold F3 monogram."""
    img, d = new_tile(NAVY)
    center_text(d, 165, 175, "F", T(230, BLACK), WHITE)
    center_text(d, 250, 175, "3", T(230, BLACK), YELLOW)
    center_text(d, 180, 315, "Fri3d Friends", T(30, BOLD), TEAL)
    return finish(img, "03_f3_monogram.png")


def logo04_node_network():
    """A little friend graph — connected dots."""
    img, d = new_tile(NAVY)
    pts = [(110, 120, TEAL), (250, 100, YELLOW), (290, 220, PINK),
           (170, 250, BLUE), (90, 210, GREEN)]
    # edges
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if (i + j) % 2 == 0:
                d.line([pts[i][0] * SS, pts[i][1] * SS, pts[j][0] * SS, pts[j][1] * SS],
                       fill=(90, 100, 130), width=5 * SS)
    for (x, y, c) in pts:
        d.ellipse([(x - 22) * SS, (y - 22) * SS, (x + 22) * SS, (y + 22) * SS], fill=c)
    center_text(d, 180, 315, "!Fri3d Friends", T(26, BOLD), WHITE)
    return finish(img, "04_node_network.png")


def logo05_badge_bump():
    """Two rounded badges tilted toward each other with a spark."""
    img, d = new_tile(NAVY)
    def badge(cx, cy, ang, col):
        b = Image.new("RGBA", (150 * SS, 150 * SS), (0, 0, 0, 0))
        bd = ImageDraw.Draw(b)
        bd.rounded_rectangle([10 * SS, 10 * SS, 140 * SS, 140 * SS], radius=30 * SS, fill=col)
        bd.ellipse([55 * SS, 45 * SS, 95 * SS, 85 * SS], fill=NAVY)     # a "face" dot
        b = b.rotate(ang, resample=Image.BICUBIC, expand=True)
        img.alpha_composite(b, (int(cx * SS - b.width / 2), int(cy * SS - b.height / 2)))
    badge(120, 165, 18, TEAL)
    badge(250, 175, -18, YELLOW)
    # spark
    sx, sy = 187, 168
    for a in range(0, 360, 45):
        r1, r2 = 12, 30
        d.line([(sx + r1 * math.cos(math.radians(a))) * SS, (sy + r1 * math.sin(math.radians(a))) * SS,
                (sx + r2 * math.cos(math.radians(a))) * SS, (sy + r2 * math.sin(math.radians(a))) * SS],
               fill=PINK, width=7 * SS)
    center_text(d, 180, 315, "!Fri3d Friends", T(26, BOLD), WHITE)
    return finish(img, "05_badge_bump.png")


def logo06_pixel_people():
    """Two retro pixel figures (Fri3d arcade vibe)."""
    img, d = new_tile(NAVY2)
    px = 22
    def figure(ox, oy, col):
        grid = [
            "..XX..",
            "..XX..",
            ".XXXX.",
            "XXXXXX",
            "X.XX.X",
            "..XX..",
            ".X..X.",
        ]
        for r, row in enumerate(grid):
            for c, ch in enumerate(row):
                if ch == "X":
                    x = ox + c * px
                    y = oy + r * px
                    d.rectangle([x * SS, y * SS, (x + px - 3) * SS, (y + px - 3) * SS], fill=col)
    figure(70, 95, TEAL)
    figure(210, 95, YELLOW)
    center_text(d, 180, 315, "!Fri3d Friends", T(26, BOLD), WHITE)
    return finish(img, "06_pixel_people.png")


def logo07_venn_rings():
    """Overlapping rings = shared groups / friends."""
    img, d = new_tile(NAVY)
    d.ellipse([55 * SS, 95 * SS, 215 * SS, 255 * SS], outline=TEAL, width=18 * SS)
    d.ellipse([145 * SS, 95 * SS, 305 * SS, 255 * SS], outline=YELLOW, width=18 * SS)
    d.ellipse([170 * SS, 160 * SS, 190 * SS, 180 * SS], fill=PINK)     # dot in overlap
    center_text(d, 180, 315, "!Fri3d Friends", T(26, BOLD), WHITE)
    return finish(img, "07_venn_rings.png")


def logo08_wordmark():
    """Stacked wordmark with a highlighted 3."""
    img, d = new_tile(NAVY)
    center_text(d, 40, 120, "!Fri", T(72, BLACK), WHITE, anchor="lm")
    center_text(d, 205, 120, "3", T(72, BLACK), YELLOW, anchor="lm")
    center_text(d, 248, 120, "d", T(72, BLACK), WHITE, anchor="lm")
    center_text(d, 180, 210, "FRIENDS", T(58, BLACK), TEAL)
    d.rounded_rectangle([70 * SS, 265 * SS, 290 * SS, 285 * SS], radius=10 * SS, fill=PINK)
    return finish(img, "08_wordmark.png")


def logo09_radar():
    """Radar finder with blips."""
    img, d = new_tile(NAVY2)
    cx, cy, rr = 180, 175, 120
    for k in (0.4, 0.7, 1.0):
        d.ellipse([(cx - rr * k) * SS, (cy - rr * k) * SS, (cx + rr * k) * SS, (cy + rr * k) * SS],
                  outline=(60, 90, 90), width=5 * SS)
    d.line([cx * SS, cy * SS, cx * SS, (cy - rr) * SS], fill=TEAL, width=8 * SS)
    d.line([cx * SS, cy * SS, (cx + rr * 0.9) * SS, (cy - rr * 0.5) * SS], fill=TEAL, width=8 * SS)
    d.pieslice([(cx - rr) * SS, (cy - rr) * SS, (cx + rr) * SS, (cy + rr) * SS],
               start=-90, end=-56, fill=(34, 211, 197, 90))
    for (bx, by, col) in ((230, 130, YELLOW), (120, 140, PINK), (200, 220, BLUE)):
        d.ellipse([(bx - 12) * SS, (by - 12) * SS, (bx + 12) * SS, (by + 12) * SS], fill=col)
    center_text(d, 180, 320, "!Fri3d Friends", T(26, BOLD), WHITE)
    return finish(img, "09_radar.png")


def logo10_arcs_connect():
    """Two C-arcs interlocking — connection/link, forming an abstract 3."""
    img, d = new_tile(NAVY)
    cx, cy, rr = 180, 165, 78
    d.arc([(cx - rr - 34) * SS, (cy - rr) * SS, (cx + rr - 34) * SS, (cy + rr) * SS],
          start=300, end=120, fill=TEAL, width=26 * SS)
    d.arc([(cx - rr + 34) * SS, (cy - rr) * SS, (cx + rr + 34) * SS, (cy + rr) * SS],
          start=120, end=300, fill=YELLOW, width=26 * SS)
    d.ellipse([(cx - 14) * SS, (cy - 14) * SS, (cx + 14) * SS, (cy + 14) * SS], fill=PINK)
    center_text(d, 180, 315, "!Fri3d Friends", T(26, BOLD), WHITE)
    return finish(img, "10_arcs_connect.png")


names = [
    logo01_signal_heart(), logo02_chat_bubbles(), logo03_f3_monogram(),
    logo04_node_network(), logo05_badge_bump(), logo06_pixel_people(),
    logo07_venn_rings(), logo08_wordmark(), logo09_radar(), logo10_arcs_connect(),
]

# contact sheet (2 rows x 5)
cols, rows, cell, pad = 5, 2, 200, 16
sheet = Image.new("RGB", (cols * cell + pad, rows * cell + pad + 40 * rows), (10, 12, 18))
sd = ImageDraw.Draw(sheet)
lab = ImageFont.truetype(BOLD, 22)
for i, n in enumerate(names):
    im = Image.open(os.path.join(OUT, n)).convert("RGBA").resize((cell - pad, cell - pad))
    r, c = divmod(i, cols)
    x = pad + c * cell
    y = pad + r * (cell + 40)
    bg = Image.new("RGBA", im.size, (10, 12, 18, 255)); bg.alpha_composite(im)
    sheet.paste(bg.convert("RGB"), (x, y))
    sd.text((x + (cell - pad) / 2, y + cell - pad + 18), "%d" % (i + 1), font=lab, fill=(255, 214, 77), anchor="mm")
sheet.save(os.path.join(OUT, "_contact_sheet.png"))

# html gallery
html = ["<html><head><meta charset=utf-8><title>!Fri3d Friends logos</title>",
        "<style>body{background:#0b0e14;color:#e6e6e6;font-family:system-ui;text-align:center}",
        "img{width:220px;margin:8px;background:#0b0e14;border:1px solid #28324a;border-radius:12px}",
        ".c{display:inline-block;margin:10px}h1{color:#ffe066}</style></head><body>",
        "<h1>!Fri3d Friends — logo proposals</h1><p>Pick a number.</p>"]
for i, n in enumerate(names):
    html.append("<div class=c><div>#%d %s</div><img src='%s'></div>" % (i + 1, n[3:-4], n))
html.append("</body></html>")
open(os.path.join(OUT, "index.html"), "w").write("\n".join(html))
print("wrote", len(names), "logos +", "contact sheet + index.html to", OUT)
