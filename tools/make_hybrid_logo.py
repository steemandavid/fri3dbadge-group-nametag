#!/usr/bin/env python3
"""Hybrid logo: #5 badge-bump x #6 pixel-people.

Two rounded badges tilting toward each other (the contact swap), each showing a
pixel-art friend on its screen, with a spark where they meet. Renders a 360px
preview into tools/logo_proposals/11_hybrid.png.
"""
import os
import math
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "logo_proposals")
os.makedirs(OUT, exist_ok=True)
S, R, SS = 360, 64, 4
NAVY, NAVY2 = (20, 24, 38), (12, 15, 23)
WHITE, YELLOW, TEAL, PINK = (245, 247, 250), (255, 214, 77), (34, 211, 197), (255, 92, 122)
BOLD = "/tmp/Montserrat-Bold.ttf"

PERSON = ["..XX..", "..XX..", ".XXXX.", "XXXXXX", "X.XX.X", "..XX..", ".X..X."]


def render(with_text=True, tile=True):
    img = Image.new("RGBA", (S * SS, S * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if tile:
        d.rounded_rectangle([0, 0, S * SS - 1, S * SS - 1], radius=R * SS, fill=NAVY)

    def badge(cx, cy, ang, bcol, pcol):
        b = Image.new("RGBA", (176 * SS, 176 * SS), (0, 0, 0, 0))
        bd = ImageDraw.Draw(b)
        bd.rounded_rectangle([16 * SS, 16 * SS, 160 * SS, 160 * SS], radius=36 * SS, fill=bcol)
        bd.rounded_rectangle([34 * SS, 34 * SS, 142 * SS, 142 * SS], radius=22 * SS, fill=NAVY2)
        px = 13
        gx = 88 - len(PERSON[0]) * px / 2
        gy = 88 - len(PERSON) * px / 2
        for r, row in enumerate(PERSON):
            for c, ch in enumerate(row):
                if ch == "X":
                    x, y = gx + c * px, gy + r * px
                    bd.rectangle([x * SS, y * SS, (x + px - 3) * SS, (y + px - 3) * SS], fill=pcol)
        b = b.rotate(ang, resample=Image.BICUBIC, expand=True)
        img.alpha_composite(b, (int(cx * SS - b.width / 2), int(cy * SS - b.height / 2)))

    ty = 150 if with_text else 180
    badge(112, ty, 15, TEAL, YELLOW)
    badge(248, ty + 12, -15, YELLOW, TEAL)
    # spark where they meet
    sx, sy = 180, ty + 4
    for a in range(0, 360, 45):
        r1, r2 = (12, 30) if a % 90 == 0 else (10, 22)
        d.line([(sx + r1 * math.cos(math.radians(a))) * SS, (sy + r1 * math.sin(math.radians(a))) * SS,
                (sx + r2 * math.cos(math.radians(a))) * SS, (sy + r2 * math.sin(math.radians(a))) * SS],
               fill=PINK, width=8 * SS)
    d.ellipse([(sx - 7) * SS, (sy - 7) * SS, (sx + 7) * SS, (sy + 7) * SS], fill=WHITE)
    if with_text:
        d.text((180 * SS, 315 * SS), "!Fri3d Friends", font=ImageFont.truetype(BOLD, 27 * SS),
               fill=WHITE, anchor="mm")
    return img.resize((S, S), Image.LANCZOS)


render(True).save(os.path.join(OUT, "11_hybrid.png"))            # preview (text + tile)

# Final app assets
APP = os.path.join(os.path.dirname(__file__), "..", "app", "com.fri3dcamp.fri3dfriends")


def make_icon(scale=0.80, top_pad_px=3):
    """Launcher icon: shrink the WHOLE tile (black square + badges) and leave
    transparent padding around it — more at the bottom — so the icon graphic is
    shorter and no longer overlaps the app-name label under it in the OS menu."""
    full = render(with_text=False, tile=True)           # 360x360 full-bleed tile + badges
    sz = int(S * scale)
    small = full.resize((sz, sz), Image.LANCZOS)
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))    # transparent
    x = (S - sz) // 2
    y = int(top_pad_px / 64 * S)                        # small top pad -> bigger bottom pad
    canvas.alpha_composite(small, (x, y))
    return canvas.resize((64, 64), Image.LANCZOS)


make_icon().save(os.path.join(APP, "icon_64x64.png"))
# Splash logo: 96px, tileless (pops on the dark splash), no text.
render(with_text=False, tile=False).resize((96, 96), Image.LANCZOS).save(os.path.join(APP, "fri3dfriends.png"))
print("wrote preview + app icon_64x64.png + fri3dfriends.png")
