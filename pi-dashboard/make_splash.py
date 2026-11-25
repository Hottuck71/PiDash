"""Draws the splash screen pictures (needs Pillow). Run again after changing them:

  python make_splash.py

Writes boot/pidash/splash/*.bin (for pidash-splash) and app/static/splash.png.
"""
import os
import struct
import zlib

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "boot", "pidash", "splash")
W, H, K = 760, 440, 3  # drawn 3 times bigger, then made small: smooth edges
BG, INK, MUTED, BLUE, SUN = (13, 17, 23), (240, 246, 252), (139, 148, 158), (79, 195, 247), (255, 202, 87)
FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size * K)


PICTURES = {
    "start": ["Starting…"],
    "setup": ["Setting up for the first time", "This takes 15 to 30 minutes. Please keep it plugged in."],
    "update": ["Installing an update", "This takes a few minutes. Please keep it plugged in."],
    "wifi": ["No internet", "Check the Wi-Fi name and password in dashboard-settings.txt. Trying again…"],
    "retry": ["The download did not finish", "Trying again in a minute…"],
}


def draw(lines):
    im = Image.new("RGB", (W * K, H * K), BG)
    d = ImageDraw.Draw(im)

    def box(x0, y0, x1, y1, r, fill):
        d.rounded_rectangle([v * K for v in (x0, y0, x1, y1)], r * K, fill=fill)

    # a calendar page with the sun looking over its corner
    cx, top = W // 2, 40
    d.ellipse([(cx + 26) * K, (top - 18) * K, (cx + 106) * K, (top + 62) * K], fill=SUN)
    box(cx - 78, top + 10, cx + 78, top + 156, 22, BG)          # gap around the page
    box(cx - 72, top + 16, cx + 72, top + 150, 18, (33, 41, 54))
    box(cx - 72, top + 16, cx + 72, top + 66, 18, BLUE)
    d.rectangle([(cx - 72) * K, (top + 48) * K, (cx + 72) * K, (top + 56) * K], fill=BLUE)
    d.rectangle([(cx - 72) * K, (top + 56) * K, (cx + 72) * K, (top + 68) * K], fill=(33, 41, 54))
    for x in (-34, 34):
        box(cx + x - 5, top + 4, cx + x + 5, top + 32, 5, INK)
    for r in range(3):
        for c in range(5):
            x, y = cx - 56 + c * 24, top + 70 + r * 25
            on = (r, c) in ((1, 2), (2, 4))
            box(x, y, x + 16, y + 16, 5, SUN if (r, c) == (1, 2) else BLUE if on else (72, 84, 102))
    mid = W * K // 2
    d.text((mid, (top + 168) * K), "PiDash", font=font("seguisb.ttf", 64), fill=INK, anchor="ma")
    y = top + 276
    for i, line in enumerate(lines):
        f = font("segoeui.ttf", 24 if i == 0 else 19)
        d.text((mid, y * K), line, font=f, fill=BLUE if i == 0 and len(lines) > 1 else MUTED, anchor="ma")
        assert d.textlength(line, font=f) < (W - 20) * K, line
        y += 42
    return im


os.makedirs(OUT, exist_ok=True)
for name, lines in PICTURES.items():
    for big in (0, 1):  # the big one is for 1080p screens
        w, h = (W * 3 // 2, H * 3 // 2) if big else (W, H)
        p = draw(lines).resize((w, h), Image.LANCZOS).quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        pal = (p.getpalette() + [0] * 768)[:768]
        px = p.tobytes()
        bg = px[0]
        assert px[-1] == bg
        pal[bg * 3:bg * 3 + 3] = BG
        with open(os.path.join(OUT, name + ("-big.bin" if big else ".bin")), "wb") as f:
            f.write(zlib.compress(struct.pack("<HH", w, h) + bytes(pal) + px, 9))
        if name == "start" and not big:
            p.putpalette(pal)
            p.save(os.path.join(HERE, "app", "static", "splash.png"), optimize=True)
        print(name, w, h)
