"""STATIC layer of the certificate: background, radio objects, logo, medal (empty or filled).
No activation data here, apart from the logo and the medal's ranking."""

import math
import os
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.colors import HexColor, white, Color
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_FONTS_OK = False


def register_fonts():
    global _FONTS_OK
    if _FONTS_OK:
        return
    for n in ("Regular", "Medium", "SemiBold", "Bold", "ExtraBold"):
        pdfmetrics.registerFont(TTFont("Pop" + n, f"{ASSETS}/fonts/Poppins-{n}.ttf"))
    pdfmetrics.registerFont(TTFont("Script", f"{ASSETS}/fonts/GreatVibes-Regular.ttf"))
    _FONTS_OK = True


W, H = landscape(A4)  # 841.89 x 595.28 pt
NAVY, NAVY2, NAVY3 = HexColor("#2B3547"), HexColor("#1D2533"), HexColor("#5A6478")
GOLD, GOLD2, GOLDL = HexColor("#C9982E"), HexColor("#E9C46A"), HexColor("#F6E3A8")
RED, RED2 = HexColor("#B0202B"), HexColor("#7E131B")
GREY = HexColor("#4A4F5A")


def A(col, a):
    return Color(col.red, col.green, col.blue, alpha=a)


# ------------------------------------------------------------------ background
def background(c):
    c.setFillColor(white)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    # large navy shape at the top right + grey-blue edge
    for col, dx in ((NAVY3, -22), (NAVY, 0)):
        p = c.beginPath()
        p.moveTo(455 + dx, H)
        p.lineTo(W, H)
        p.lineTo(W, 320 + dx * 1.5)
        p.curveTo(760, 400 + dx, 640, H - 30, 455 + dx, H)
        p.close()
        c.setFillColor(col)
        c.drawPath(p, stroke=0, fill=1)
    # gold band at the bottom right (gradient)
    c.saveState()
    p = c.beginPath()
    p.moveTo(400, 0)
    p.curveTo(560, 70, 700, 95, W, 150)
    p.lineTo(W, 0)
    p.close()
    c.clipPath(p, stroke=0, fill=0)
    c.linearGradient(400, 0, W, 150, (GOLDL, GOLD2), extend=True)
    c.restoreState()
    c.setStrokeColor(GOLD)
    c.setLineWidth(2)
    p = c.beginPath()
    p.moveTo(380, 0)
    p.curveTo(550, 80, 700, 108, W, 165)
    c.drawPath(p, stroke=1, fill=0)
    # navy arc + gold swoosh on the left
    c.saveState()
    p = c.beginPath()
    p.rect(0, 0, W, H)
    c.clipPath(p, stroke=0, fill=0)
    cx, cy = -300, H / 2 + 20
    c.setFillColor(NAVY)
    c.circle(cx, cy, 480, stroke=0, fill=1)
    c.setFillColor(NAVY2)
    c.circle(cx - 40, cy - 30, 430, stroke=0, fill=1)
    c.setStrokeColor(GOLD)
    c.setLineWidth(16)
    c.circle(cx, cy, 494, stroke=1, fill=0)
    c.setStrokeColor(GOLD2)
    c.setLineWidth(4)
    c.circle(cx, cy, 505, stroke=1, fill=0)
    c.restoreState()


# ------------------------------------------------------------------ radio objects
def microphone(c, x, y, s=1):
    c.saveState()
    c.translate(x, y)
    c.scale(s, s)
    c.rotate(-12)
    c.setFillColor(HexColor("#0F141D"))
    c.ellipse(-34, -6, 34, 8, stroke=0, fill=1)
    c.setFillColor(GOLD)
    c.ellipse(-34, 2, 34, 12, stroke=0, fill=1)
    c.setFillColor(HexColor("#0F141D"))
    c.ellipse(-30, 4, 30, 12, stroke=0, fill=1)
    c.setStrokeColor(HexColor("#0F141D"))
    c.setLineWidth(5)
    c.line(0, 8, 0, 40)
    c.setStrokeColor(GOLD)
    c.setLineWidth(3)
    c.arc(-26, 32, 26, 84, 180, 180)
    c.setFillColor(HexColor("#161C27"))
    c.roundRect(-18, 40, 36, 62, 18, stroke=0, fill=1)
    c.setStrokeColor(A(GOLD2, 0.8))
    c.setLineWidth(0.8)
    for k in range(8):
        c.line(-13, 52 + k * 6, 13, 52 + k * 6)
    c.setFillColor(GOLD)
    c.rect(-19, 62, 38, 5, stroke=0, fill=1)
    c.restoreState()


def morse_key(c, x, y, s=1):
    c.saveState()
    c.translate(x, y)
    c.scale(s, s)
    c.rotate(8)
    c.setFillColor(HexColor("#0F141D"))
    c.roundRect(-60, 0, 120, 16, 4, stroke=0, fill=1)
    c.setFillColor(GOLD)
    c.rect(-60, 12, 120, 4, stroke=0, fill=1)
    c.setFillColor(HexColor("#161C27"))
    c.rect(-40, 16, 14, 16, stroke=0, fill=1)
    c.setStrokeColor(GOLD)
    c.setLineWidth(5)
    c.line(-38, 34, 52, 30)
    c.setFillColor(HexColor("#0F141D"))
    c.ellipse(34, 30, 70, 42, stroke=0, fill=1)
    c.setFillColor(GOLD2)
    c.ellipse(38, 38, 66, 44, stroke=0, fill=1)
    c.setFillColor(GOLD)
    c.circle(10, 24, 4, stroke=0, fill=1)
    c.restoreState()


def yagi(c, x, y, s=1):
    c.saveState()
    c.translate(x, y)
    c.scale(s, s)
    c.rotate(18)
    c.setStrokeColor(NAVY2)
    c.setLineWidth(3)
    c.line(0, -60, 0, 0)
    c.setLineWidth(2.5)
    c.line(-55, 0, 55, 0)
    c.setStrokeColor(GOLD)
    c.setLineWidth(2)
    for k in range(7):
        ex = -50 + k * 16.6
        el = 30 - k * 2.2
        c.line(ex, -el / 2, ex, el / 2)
    c.restoreState()


def radio_set(c, x, y, s=1):
    """Radio set: cabinet, display, S-meter, VFO and loudspeaker."""
    c.saveState()
    c.translate(x, y)
    c.scale(s, s)
    c.setFillColor(A(HexColor("#000000"), 0.18))
    c.roundRect(-86, -50, 180, 92, 9, stroke=0, fill=1)
    c.setFillColor(HexColor("#161C27"))
    c.roundRect(-90, -46, 180, 92, 9, stroke=0, fill=1)
    c.setFillColor(NAVY3)
    c.roundRect(-82, -38, 164, 76, 6, stroke=0, fill=1)
    # display: frequency and S-meter
    c.setFillColor(GOLDL)
    c.roundRect(-72, -6, 96, 36, 4, stroke=0, fill=1)
    c.setFillColor(NAVY2)
    c.setFont("PopBold", 13)
    c.drawString(-66, 12, "14.190")
    c.setStrokeColor(NAVY3)
    c.setLineWidth(1.4)
    c.line(-66, 4, 16, 4)
    c.setStrokeColor(RED)
    c.setLineWidth(1.6)
    for k in range(6):
        c.line(-64 + k * 8, 0, -64 + k * 8, 3 + k)
    # big tuning knob (VFO) and two small ones
    c.setFillColor(GOLD)
    c.circle(56, 6, 21, stroke=0, fill=1)
    c.setFillColor(NAVY2)
    c.circle(56, 6, 15, stroke=0, fill=1)
    c.setFillColor(GOLD2)
    c.roundRect(53, 12, 6, 12, 3, stroke=0, fill=1)
    for dx in (-60, -42):
        c.setFillColor(GOLD2)
        c.circle(dx, -22, 7, stroke=0, fill=1)
        c.setFillColor(NAVY2)
        c.circle(dx, -22, 3, stroke=0, fill=1)
    # loudspeaker
    c.setStrokeColor(A(GOLD2, 0.85))
    c.setLineWidth(1.6)
    for k in range(5):
        c.line(-24, -30 + k * 4, 24, -30 + k * 4)
    # handle
    c.setStrokeColor(GOLD)
    c.setLineWidth(3.2)
    c.line(-70, 46, -70, 56)
    c.line(-70, 56, 70, 56)
    c.line(70, 56, 70, 46)
    c.restoreState()


def star(c, x, y, r, col):
    p = c.beginPath()
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.42
        a = math.pi / 2 + i * math.pi / 5
        (p.moveTo if i == 0 else p.lineTo)(x + rr * math.cos(a), y + rr * math.sin(a))
    p.close()
    c.setFillColor(col)
    c.drawPath(p, stroke=0, fill=1)


def waves(c, x, y, n=3):
    c.setLineWidth(2)
    for i in range(n):
        c.setStrokeColor(A(GOLD2, 0.9 - i * 0.25))
        r = 12 + i * 10
        c.arc(x - r, y - r, x + r, y + r, -40, 80)


# ------------------------------------------------------------------ badges
def serration(c, cx, cy, r1, r2, n, col):
    p = c.beginPath()
    for i in range(2 * n):
        r = r1 if i % 2 == 0 else r2
        a = math.pi * i / n
        (p.moveTo if i == 0 else p.lineTo)(cx + r * math.cos(a), cy + r * math.sin(a))
    p.close()
    c.setFillColor(col)
    c.drawPath(p, stroke=0, fill=1)


def medal(c, position, total, cx, cy):
    """Medal with red ribbons: "1er / SUR 126". position=None → medal without a ranking."""
    for s in (-1, 1):
        c.setFillColor(RED if s < 0 else RED2)
        p = c.beginPath()
        p.moveTo(cx + s * 8, cy - 30)
        p.lineTo(cx + s * 44, cy - 108)
        p.lineTo(cx + s * 28, cy - 98)
        p.lineTo(cx + s * 20, cy - 116)
        p.lineTo(cx - s * 16, cy - 36)
        p.close()
        c.drawPath(p, stroke=0, fill=1)
    c.setFillColor(A(HexColor("#000000"), 0.25))
    c.circle(cx + 3, cy - 4, 58, stroke=0, fill=1)
    serration(c, cx, cy, 58, 53, 34, GOLD)
    c.setFillColor(GOLD2)
    c.circle(cx, cy, 50, stroke=0, fill=1)
    c.setFillColor(NAVY2)
    c.circle(cx, cy, 40, stroke=0, fill=1)
    c.setFillColor(GOLDL)
    c.circle(cx, cy, 34, stroke=0, fill=1)
    if position is None:
        c.setFillColor(RED2)
        c.setFont("PopExtraBold", 13)
        c.drawCentredString(cx, cy - 4, "QSO")
        return
    num = str(position)
    suffix = "er" if position == 1 else "e"  # French ordinal
    fs = 38 if len(num) == 1 else (30 if len(num) == 2 else 22)
    w = pdfmetrics.stringWidth(num, "PopExtraBold", fs)
    sw = pdfmetrics.stringWidth(suffix, "PopBold", fs * 0.37)
    x = cx - (w + sw) / 2
    c.setFillColor(RED2)
    c.setFont("PopExtraBold", fs)
    c.drawString(x, cy - fs * 0.21, num)
    c.setFont("PopBold", fs * 0.37)
    c.drawString(x + w, cy + fs * 0.3, suffix)
    if total:
        c.setFillColor(NAVY2)
        c.setFont("PopBold", 8)
        c.drawCentredString(cx, cy - 20, f"SUR {total}")


def logo(c, cx, cy, diam, path=None):
    # Fallback: neutral emblem shipped with the module (the club logo, when
    # there is one, comes through ``logo_path``).
    img = ImageReader(path or f"{ASSETS}/logo.png")
    iw, ih = img.getSize()
    w = diam
    h = diam * ih / iw
    c.setFillColor(A(HexColor("#000000"), 0.3))
    c.circle(cx + 3, cy - 4, diam / 2 + 5, stroke=0, fill=1)
    serration(c, cx, cy, diam / 2 + 9, diam / 2 + 4, 40, GOLD)
    c.setFillColor(GOLD2)
    c.circle(cx, cy, diam / 2 + 3, stroke=0, fill=1)
    if (path or "").lower().endswith(".png"):
        c.drawImage(img, cx - w / 2, cy - h / 2, w, h, mask="auto")
        return
    # JPEG flattened on a gold background: much lighter, clipped to a disc
    c.saveState()
    pth = c.beginPath()
    pth.circle(cx, cy, diam / 2 + 2)
    c.clipPath(pth, stroke=0, fill=0)
    c.drawImage(img, cx - w / 2, cy - h / 2, w, h)
    c.restoreState()


def border(c, colors=None, margin=7, thickness=1.3):
    """Thin border around the page: three concentric lines.

    Blue-white-red by default; the caller picks the colours (club or country
    flag, or anything else)."""
    colors = colors or ("#0055A4", "#FFFFFF", "#EF3340")
    for i, col in enumerate(colors):
        d = margin + i * (thickness + 0.6)
        c.setStrokeColor(HexColor(col))
        c.setLineWidth(thickness)
        c.rect(d, d, W - 2 * d, H - 2 * d, stroke=1, fill=0)


def flag(c, path, x, y, height=30):
    """Flag thumbnail (PNG), left-aligned on x, vertically centred on y.

    Flag thumbnails are palette PNGs: drawn as they are, they come out washed
    out. They are converted to RGB first."""
    from PIL import Image

    with Image.open(path) as src:
        img = ImageReader(src.convert("RGB"))
    iw, ih = img.getSize()
    w = height * iw / ih
    # The drop shadow is drawn in an isolated state: otherwise its alpha stays
    # in the brush and the flag comes out washed out.
    c.saveState()
    c.setFillColor(A(HexColor("#000000"), 0.18))
    c.rect(x + 1.5, y - height / 2 - 1.5, w, height, stroke=0, fill=1)
    c.restoreState()
    c.drawImage(img, x, y - height / 2, w, height, mask="auto")
    c.setStrokeColor(NAVY3)
    c.setLineWidth(0.7)
    c.rect(x, y - height / 2, w, height, stroke=1, fill=0)
    return w


def _lightning(c, x, y, ang, length, col):
    """Small zigzag lightning bolt starting at (x, y), pointing at ``ang`` (degrees)."""
    c.saveState()
    c.translate(x, y)
    c.rotate(ang)
    p = c.beginPath()
    for i, (px, py) in enumerate(
        [
            (0, 1.2),
            (length * 0.55, 2.4),
            (length * 0.45, 0.2),
            (length, 1.4),
            (length * 0.5, -1.4),
            (length * 0.6, 0.9),
            (0, -1.2),
        ]
    ):
        (p.moveTo if i == 0 else p.lineTo)(px, py)
    p.close()
    c.setFillColor(col)
    c.drawPath(p, stroke=0, fill=1)
    c.restoreState()


def emblem(c, cx, cy, text="HAM RADIO", s=1):
    """Amateur radio emblem: a mast with lightning bolts inside an arc, above a
    winged banner carrying ``text`` (the club name, otherwise "HAM RADIO").
    (cx, cy) = middle of the banner."""
    c.saveState()
    c.translate(cx, cy)
    c.scale(s, s)
    # arc open at the bottom, behind the mast
    c.setStrokeColor(GOLD)
    c.setLineWidth(2.2)
    c.arc(-40, -8, 40, 72, -22, 224)
    # wings: three fanned feathers on each side, behind the banner
    for sx in (-1, 1):
        for k in range(3):
            c.saveState()
            c.translate(sx * 33, 11)
            c.rotate(sx * (12 + k * 20))
            c.setFillColor(GOLD if k % 2 == 0 else GOLD2)
            ln = 28 - k * 4
            p = c.beginPath()
            p.moveTo(0, 2.8)
            p.lineTo(sx * (ln - 5), 3.4)
            p.curveTo(sx * (ln + 1), 3.4, sx * (ln + 1), -3.4, sx * (ln - 5), -3.4)
            p.lineTo(0, -2.8)
            p.close()
            c.drawPath(p, stroke=0, fill=1)
            c.restoreState()
    # mound and lattice mast
    c.setFillColor(NAVY2)
    p = c.beginPath()
    p.moveTo(-24, 6)
    p.curveTo(-16, 16, 16, 16, 24, 6)
    p.close()
    c.drawPath(p, stroke=0, fill=1)
    c.setStrokeColor(NAVY2)
    c.setLineWidth(1.6)
    c.line(-11, 10, -1.6, 58)
    c.line(11, 10, 1.6, 58)
    c.setLineWidth(0.9)
    levels = [10, 22, 33, 43, 51, 58]

    def half(y):
        return 11 - (y - 10) * (9.4 / 48)

    for a, b in zip(levels, levels[1:]):
        c.line(-half(a), a, half(b), b)
        c.line(half(a), a, -half(b), b)
        c.line(-half(b), b, half(b), b)
    c.setFillColor(RED)
    c.circle(0, 61, 2.6, stroke=0, fill=1)
    for ang, d, ln in [(62, 6, 13), (118, 6, 13), (26, 6, 15), (154, 6, 15)]:
        r = math.radians(ang)
        _lightning(c, d * math.cos(r), 61 + d * math.sin(r), ang, ln, GOLD)
    # smiling banner (centre of curvature above), ends folded behind
    R, ep, half_len = 150.0, 8.5, 50.0
    tm = half_len / R
    for sx in (-1, 1):
        a = sx * tm
        bx, by = R * math.sin(a), R - R * math.cos(a)
        c.setFillColor(NAVY3)
        p = c.beginPath()
        p.moveTo(bx - sx * 6, by + ep - 5)
        p.lineTo(bx + sx * 12, by + ep - 3)
        p.lineTo(bx + sx * 7, by - 2)
        p.lineTo(bx + sx * 12, by - ep - 5)
        p.lineTo(bx - sx * 6, by - ep - 5)
        p.close()
        c.drawPath(p, stroke=0, fill=1)

    def pts(r):
        return [(r * math.sin(t), R - r * math.cos(t)) for t in (-tm + 2 * tm * i / 24 for i in range(25))]

    top, bottom = pts(R - ep), pts(R + ep)
    p = c.beginPath()
    p.moveTo(*top[0])
    for pt in top[1:]:
        p.lineTo(*pt)
    for pt in reversed(bottom):
        p.lineTo(*pt)
    p.close()
    c.setFillColor(NAVY)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.2)
    c.drawPath(p, stroke=1, fill=1)
    # text along the banner, shrunk to fit between the ends
    text = (text or "HAM RADIO").upper()
    fs = 9.0
    while fs > 4.5 and pdfmetrics.stringWidth(text, "PopBold", fs) > 2 * half_len - 10:
        fs -= 0.25
    rt = R + fs * 0.34  # baseline radius
    t = -pdfmetrics.stringWidth(text, "PopBold", fs) / 2 / rt
    c.setFillColor(white)
    c.setFont("PopBold", fs)
    for ch in text:
        w = pdfmetrics.stringWidth(ch, "PopBold", fs)
        tc = t + w / 2 / rt
        c.saveState()
        c.translate(rt * math.sin(tc), R - rt * math.cos(tc))
        c.rotate(math.degrees(tc))
        c.drawCentredString(0, 0, ch)
        c.restoreState()
        t += w / rt
    c.restoreState()


def ham_symbol(c, cx, cy, h=56):
    """International amateur radio symbol: diamond, antenna, coil, ground."""
    w = h * 0.52
    c.saveState()
    c.translate(cx, cy)

    def diamond(hh, ww, col):
        p = c.beginPath()
        p.moveTo(0, hh / 2)
        p.lineTo(ww / 2, 0)
        p.lineTo(0, -hh / 2)
        p.lineTo(-ww / 2, 0)
        p.close()
        c.setFillColor(col)
        c.drawPath(p, stroke=0, fill=1)

    c.saveState()
    c.translate(1.5, -1.5)
    diamond(h, w, A(HexColor("#000000"), 0.18))
    c.restoreState()
    diamond(h, w, NAVY2)
    diamond(h * 0.8, w * 0.8, GOLD2)
    u = h / 56  # drawing designed for h = 56
    c.setStrokeColor(NAVY2)
    c.setLineWidth(1.1 * u)
    c.setLineCap(1)
    c.line(0, 16 * u, 0, 9 * u)  # antenna feed
    c.line(-3.5 * u, 19 * u, 0, 13 * u)
    c.line(3.5 * u, 19 * u, 0, 13 * u)
    c.line(-3.5 * u, 19 * u, 3.5 * u, 19 * u)  # closed V antenna
    for k in range(5):  # coil
        y = 7 * u - k * 3 * u
        c.ellipse(-3 * u, y - 2.4 * u, 3 * u, y + 0.6 * u, stroke=1, fill=0)
    c.line(0, -8.5 * u, 0, -13 * u)
    for k, ln in enumerate((4.5, 3.2, 2, 0.9)):  # ground
        y = -13 * u - k * 1.9 * u
        c.line(-ln * u, y, ln * u, y)
    c.restoreState()


def decor(c, logo_path=None):
    """Draws the whole static layer (medal excluded)."""
    background(c)
    microphone(c, 70, H - 150, 1.0)
    waves(c, 110, H - 70)
    morse_key(c, 80, 55, 0.95)
    yagi(c, 150, 165, 0.6)
    radio_set(c, 745, 300, 0.78)
    logo(c, 100, 318, 172, logo_path)
    for x, y, r in [(150, H - 40, 7), (175, 110, 9), (30, 205, 5), (205, 28, 6)]:
        star(c, x, y, r, GOLD2)
