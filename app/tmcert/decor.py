"""Couche STATIQUE du certificat : décor, objets radio, logo, médaille (vide ou remplie).
Aucune donnée d'activation ici hormis le logo et le classement de la médaille."""
import math, os
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.colors import HexColor, white, Color
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_FONTS_OK = False

def register_fonts():
    global _FONTS_OK
    if _FONTS_OK: return
    for n in ("Regular", "Medium", "SemiBold", "Bold", "ExtraBold"):
        pdfmetrics.registerFont(TTFont("Pop" + n, f"{ASSETS}/fonts/Poppins-{n}.ttf"))
    pdfmetrics.registerFont(TTFont("Script", f"{ASSETS}/fonts/GreatVibes-Regular.ttf"))
    _FONTS_OK = True

W, H = landscape(A4)          # 841.89 x 595.28 pt
NAVY, NAVY2, NAVY3 = HexColor("#2B3547"), HexColor("#1D2533"), HexColor("#5A6478")
GOLD, GOLD2, GOLDL = HexColor("#C9982E"), HexColor("#E9C46A"), HexColor("#F6E3A8")
RED, RED2 = HexColor("#B0202B"), HexColor("#7E131B")
GRIS = HexColor("#4A4F5A")

def A(col, a): return Color(col.red, col.green, col.blue, alpha=a)

# ------------------------------------------------------------------ fond
def fond(c):
    c.setFillColor(white); c.rect(0, 0, W, H, stroke=0, fill=1)
    # grande forme navy en haut à droite + liseré gris-bleu
    for col, dx in ((NAVY3, -22), (NAVY, 0)):
        p = c.beginPath(); p.moveTo(455 + dx, H); p.lineTo(W, H); p.lineTo(W, 320 + dx * 1.5)
        p.curveTo(760, 400 + dx, 640, H - 30, 455 + dx, H); p.close()
        c.setFillColor(col); c.drawPath(p, stroke=0, fill=1)
    # bande or en bas à droite (dégradé)
    c.saveState()
    p = c.beginPath(); p.moveTo(400, 0); p.curveTo(560, 70, 700, 95, W, 150); p.lineTo(W, 0); p.close()
    c.clipPath(p, stroke=0, fill=0)
    c.linearGradient(400, 0, W, 150, (GOLDL, GOLD2), extend=True)
    c.restoreState()
    c.setStrokeColor(GOLD); c.setLineWidth(2)
    p = c.beginPath(); p.moveTo(380, 0); p.curveTo(550, 80, 700, 108, W, 165)
    c.drawPath(p, stroke=1, fill=0)
    # arc navy + swoosh or à gauche
    c.saveState()
    p = c.beginPath(); p.rect(0, 0, W, H); c.clipPath(p, stroke=0, fill=0)
    cx, cy = -300, H / 2 + 20
    c.setFillColor(NAVY); c.circle(cx, cy, 480, stroke=0, fill=1)
    c.setFillColor(NAVY2); c.circle(cx - 40, cy - 30, 430, stroke=0, fill=1)
    c.setStrokeColor(GOLD); c.setLineWidth(16); c.circle(cx, cy, 494, stroke=1, fill=0)
    c.setStrokeColor(GOLD2); c.setLineWidth(4); c.circle(cx, cy, 505, stroke=1, fill=0)
    c.restoreState()

# ------------------------------------------------------------------ objets radio
def micro(c, x, y, s=1):
    c.saveState(); c.translate(x, y); c.scale(s, s); c.rotate(-12)
    c.setFillColor(HexColor("#0F141D")); c.ellipse(-34, -6, 34, 8, stroke=0, fill=1)
    c.setFillColor(GOLD); c.ellipse(-34, 2, 34, 12, stroke=0, fill=1)
    c.setFillColor(HexColor("#0F141D")); c.ellipse(-30, 4, 30, 12, stroke=0, fill=1)
    c.setStrokeColor(HexColor("#0F141D")); c.setLineWidth(5); c.line(0, 8, 0, 40)
    c.setStrokeColor(GOLD); c.setLineWidth(3); c.arc(-26, 32, 26, 84, 180, 180)
    c.setFillColor(HexColor("#161C27")); c.roundRect(-18, 40, 36, 62, 18, stroke=0, fill=1)
    c.setStrokeColor(A(GOLD2, 0.8)); c.setLineWidth(0.8)
    for k in range(8): c.line(-13, 52 + k * 6, 13, 52 + k * 6)
    c.setFillColor(GOLD); c.rect(-19, 62, 38, 5, stroke=0, fill=1)
    c.restoreState()

def manipulateur(c, x, y, s=1):
    c.saveState(); c.translate(x, y); c.scale(s, s); c.rotate(8)
    c.setFillColor(HexColor("#0F141D")); c.roundRect(-60, 0, 120, 16, 4, stroke=0, fill=1)
    c.setFillColor(GOLD); c.rect(-60, 12, 120, 4, stroke=0, fill=1)
    c.setFillColor(HexColor("#161C27")); c.rect(-40, 16, 14, 16, stroke=0, fill=1)
    c.setStrokeColor(GOLD); c.setLineWidth(5); c.line(-38, 34, 52, 30)
    c.setFillColor(HexColor("#0F141D")); c.ellipse(34, 30, 70, 42, stroke=0, fill=1)
    c.setFillColor(GOLD2); c.ellipse(38, 38, 66, 44, stroke=0, fill=1)
    c.setFillColor(GOLD); c.circle(10, 24, 4, stroke=0, fill=1)
    c.restoreState()

def yagi(c, x, y, s=1):
    c.saveState(); c.translate(x, y); c.scale(s, s); c.rotate(18)
    c.setStrokeColor(NAVY2); c.setLineWidth(3); c.line(0, -60, 0, 0)
    c.setLineWidth(2.5); c.line(-55, 0, 55, 0)
    c.setStrokeColor(GOLD); c.setLineWidth(2)
    for k in range(7):
        ex = -50 + k * 16.6; el = 30 - k * 2.2
        c.line(ex, -el / 2, ex, el / 2)
    c.restoreState()

def poste(c, x, y, s=1):
    """Poste de radio : boîtier, écran, S-mètre, VFO et haut-parleur."""
    c.saveState(); c.translate(x, y); c.scale(s, s)
    c.setFillColor(A(HexColor("#000000"), 0.18)); c.roundRect(-86, -50, 180, 92, 9, stroke=0, fill=1)
    c.setFillColor(HexColor("#161C27")); c.roundRect(-90, -46, 180, 92, 9, stroke=0, fill=1)
    c.setFillColor(NAVY3); c.roundRect(-82, -38, 164, 76, 6, stroke=0, fill=1)
    # écran : fréquence et S-mètre
    c.setFillColor(GOLDL); c.roundRect(-72, -6, 96, 36, 4, stroke=0, fill=1)
    c.setFillColor(NAVY2); c.setFont("PopBold", 13); c.drawString(-66, 12, "14.190")
    c.setStrokeColor(NAVY3); c.setLineWidth(1.4); c.line(-66, 4, 16, 4)
    c.setStrokeColor(RED); c.setLineWidth(1.6)
    for k in range(6):
        c.line(-64 + k * 8, 0, -64 + k * 8, 3 + k)
    # gros bouton d'accord (VFO) et deux petits
    c.setFillColor(GOLD); c.circle(56, 6, 21, stroke=0, fill=1)
    c.setFillColor(NAVY2); c.circle(56, 6, 15, stroke=0, fill=1)
    c.setFillColor(GOLD2); c.roundRect(53, 12, 6, 12, 3, stroke=0, fill=1)
    for dx in (-60, -42):
        c.setFillColor(GOLD2); c.circle(dx, -22, 7, stroke=0, fill=1)
        c.setFillColor(NAVY2); c.circle(dx, -22, 3, stroke=0, fill=1)
    # haut-parleur
    c.setStrokeColor(A(GOLD2, 0.85)); c.setLineWidth(1.6)
    for k in range(5):
        c.line(-24, -30 + k * 4, 24, -30 + k * 4)
    # poignée
    c.setStrokeColor(GOLD); c.setLineWidth(3.2)
    c.line(-70, 46, -70, 56); c.line(-70, 56, 70, 56); c.line(70, 56, 70, 46)
    c.restoreState()


def etoile(c, x, y, r, col):
    p = c.beginPath()
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.42; a = math.pi / 2 + i * math.pi / 5
        (p.moveTo if i == 0 else p.lineTo)(x + rr * math.cos(a), y + rr * math.sin(a))
    p.close(); c.setFillColor(col); c.drawPath(p, stroke=0, fill=1)

def ondes(c, x, y, n=3):
    c.setLineWidth(2)
    for i in range(n):
        c.setStrokeColor(A(GOLD2, 0.9 - i * 0.25)); r = 12 + i * 10
        c.arc(x - r, y - r, x + r, y + r, -40, 80)

# ------------------------------------------------------------------ badges
def dentelure(c, cx, cy, r1, r2, n, col):
    p = c.beginPath()
    for i in range(2 * n):
        r = r1 if i % 2 == 0 else r2; a = math.pi * i / n
        (p.moveTo if i == 0 else p.lineTo)(cx + r * math.cos(a), cy + r * math.sin(a))
    p.close(); c.setFillColor(col); c.drawPath(p, stroke=0, fill=1)

def medaille(c, position, total, cx, cy):
    """Médaille à rubans rouges : « 1er / SUR 126 ». position=None → médaille sans classement."""
    for s in (-1, 1):
        c.setFillColor(RED if s < 0 else RED2)
        p = c.beginPath(); p.moveTo(cx + s * 8, cy - 30); p.lineTo(cx + s * 44, cy - 108)
        p.lineTo(cx + s * 28, cy - 98); p.lineTo(cx + s * 20, cy - 116); p.lineTo(cx - s * 16, cy - 36)
        p.close(); c.drawPath(p, stroke=0, fill=1)
    c.setFillColor(A(HexColor("#000000"), 0.25)); c.circle(cx + 3, cy - 4, 58, stroke=0, fill=1)
    dentelure(c, cx, cy, 58, 53, 34, GOLD)
    c.setFillColor(GOLD2); c.circle(cx, cy, 50, stroke=0, fill=1)
    c.setFillColor(NAVY2); c.circle(cx, cy, 40, stroke=0, fill=1)
    c.setFillColor(GOLDL); c.circle(cx, cy, 34, stroke=0, fill=1)
    if position is None:
        c.setFillColor(RED2); c.setFont("PopExtraBold", 13); c.drawCentredString(cx, cy - 4, "QSO")
        return
    num = str(position); suf = "er" if position == 1 else "e"
    fs = 38 if len(num) == 1 else (30 if len(num) == 2 else 22)
    w = pdfmetrics.stringWidth(num, "PopExtraBold", fs)
    sw = pdfmetrics.stringWidth(suf, "PopBold", fs * 0.37)
    x = cx - (w + sw) / 2
    c.setFillColor(RED2); c.setFont("PopExtraBold", fs); c.drawString(x, cy - fs * 0.21, num)
    c.setFont("PopBold", fs * 0.37); c.drawString(x + w, cy + fs * 0.3, suf)
    if total:
        c.setFillColor(NAVY2); c.setFont("PopBold", 8); c.drawCentredString(cx, cy - 20, f"SUR {total}")

def logo(c, cx, cy, diam, path=None):
    # Repli : emblème neutre livré avec le module (le logo du club, quand il y
    # en a un, arrive par ``logo_path``).
    img = ImageReader(path or f"{ASSETS}/logo.png")
    iw, ih = img.getSize(); w = diam; h = diam * ih / iw
    c.setFillColor(A(HexColor("#000000"), 0.3)); c.circle(cx + 3, cy - 4, diam / 2 + 5, stroke=0, fill=1)
    dentelure(c, cx, cy, diam / 2 + 9, diam / 2 + 4, 40, GOLD)
    c.setFillColor(GOLD2); c.circle(cx, cy, diam / 2 + 3, stroke=0, fill=1)
    if (path or "").lower().endswith(".png"):
        c.drawImage(img, cx - w / 2, cy - h / 2, w, h, mask="auto"); return
    # JPEG aplati sur fond or : bien plus léger, découpé en disque
    c.saveState(); pth = c.beginPath(); pth.circle(cx, cy, diam / 2 + 2); c.clipPath(pth, stroke=0, fill=0)
    c.drawImage(img, cx - w / 2, cy - h / 2, w, h); c.restoreState()

def liseret(c, couleurs=None, marge=7, epaisseur=1.3):
    """Fin liseré autour de la page : trois filets concentriques.

    Par défaut bleu-blanc-rouge ; l'appelant choisit ses couleurs (drapeau du
    club, du pays, ou rien du tout)."""
    couleurs = couleurs or ("#0055A4", "#FFFFFF", "#EF3340")
    for i, col in enumerate(couleurs):
        d = marge + i * (epaisseur + 0.6)
        c.setStrokeColor(HexColor(col)); c.setLineWidth(epaisseur)
        c.rect(d, d, W - 2 * d, H - 2 * d, stroke=1, fill=0)


def drapeau(c, chemin, x, y, hauteur=30):
    """Vignette de drapeau (PNG), calée à gauche sur x, centrée sur y.

    Les vignettes de drapeaux sont des PNG à palette : passées telles quelles,
    elles ressortent délavées. On les convertit en RVB avant de les poser."""
    from PIL import Image

    with Image.open(chemin) as src:
        img = ImageReader(src.convert("RGB"))
    iw, ih = img.getSize()
    w = hauteur * iw / ih
    # L'ombre portée est posée dans un état isolé : sinon son alpha reste dans
    # le pinceau et le drapeau ressort délavé.
    c.saveState()
    c.setFillColor(A(HexColor("#000000"), 0.18))
    c.rect(x + 1.5, y - hauteur / 2 - 1.5, w, hauteur, stroke=0, fill=1)
    c.restoreState()
    c.drawImage(img, x, y - hauteur / 2, w, hauteur, mask="auto")
    c.setStrokeColor(NAVY3); c.setLineWidth(0.7)
    c.rect(x, y - hauteur / 2, w, hauteur, stroke=1, fill=0)
    return w


def decor(c, logo_path=None):
    """Dessine toute la couche statique (hors médaille)."""
    fond(c)
    micro(c, 70, H - 150, 1.0)
    ondes(c, 110, H - 70)
    manipulateur(c, 80, 55, 0.95)
    yagi(c, 150, 165, 0.6)
    poste(c, 745, 300, 0.78)
    logo(c, 100, 318, 172, logo_path)
    for (x, y, r) in [(150, H - 40, 7), (175, 110, 9), (30, 205, 5), (205, 28, 6)]:
        etoile(c, x, y, r, GOLD2)
