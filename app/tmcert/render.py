"""Couche DYNAMIQUE : remplit le certificat à partir d'un dict conforme à schema/certificat.schema.json.
API :  render(data: dict) -> bytes (PDF)"""
import io
from datetime import date, datetime
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from .decor import (register_fonts, decor, medaille, W, H, A,
                    NAVY, NAVY2, NAVY3, GOLD, RED, RED2, GRIS)

MAX_LIGNES_P1 = 7          # lignes QSO visibles en page 1
LIGNES_ANNEXE = 28         # lignes QSO par page d'annexe
X0, BW = 228, 420          # colonne de contenu
MODE_COL = {"SSB": "#C9982E", "CW": "#B0202B", "FT8": "#2C6E9C", "FT4": "#2C6E9C",
            "FM": "#3B7D4F", "RTTY": "#6A4C93", "DIGI": "#2C6E9C"}
BANDES = [(1.8, 2.0, "160 m"), (3.5, 3.8, "80 m"), (5.3, 5.4, "60 m"), (7.0, 7.2, "40 m"),
          (10.1, 10.15, "30 m"), (14.0, 14.35, "20 m"), (18.068, 18.168, "17 m"),
          (21.0, 21.45, "15 m"), (24.89, 24.99, "12 m"), (28.0, 29.7, "10 m"), (50, 52, "6 m"),
          (144, 146, "2 m"), (430, 440, "70 cm"), (1240, 1300, "23 cm"), (2400, 2450, "13 cm"),
          (10000, 10500, "3 cm")]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


# ------------------------------------------------------------------ utilitaires
def bande_de(freq):
    for lo, hi, b in BANDES:
        if lo <= freq <= hi: return b
    return "?"

def fmt_freq(f):   return f"{f:,.3f}".replace(",", " ").replace(".", ",")
def fmt_date(s):
    try: return datetime.strptime(s, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError): return s or ""

def normaliser(data):
    """Complète / nettoie les données : bande, tri chronologique, points, totaux."""
    d = {k: (dict(v) if isinstance(v, dict) else v) for k, v in data.items()}
    bar = {"*": 1, **(d.get("bareme") or {})}
    qso = []
    for q in d.get("qso", []):
        q = dict(q); q["mode"] = q["mode"].upper()
        freq = q.get("freq_mhz")
        q["bande"] = q.get("bande") or (bande_de(float(freq)) if freq else "?")
        q["points"] = bar.get(q["mode"], bar["*"])
        qso.append(q)
    qso.sort(key=lambda q: (q["date"], q["heure_utc"]))
    d["qso"] = qso
    d["stats"] = dict(nb=len(qso), bandes=len({q["bande"] for q in qso}),
                      modes=sorted({q["mode"] for q in qso}),
                      points=sum(q["points"] for q in qso))
    cert = d.setdefault("certificat", {})
    cert.setdefault("date_emission", date.today().isoformat())
    return d

def fit(txt, font, size, maxw, mini):
    while size > mini and pdfmetrics.stringWidth(txt, font, size) > maxw: size -= 1
    return size

def wrap(txt, font, size, maxw):
    lignes, l = [], ""
    for m in txt.split():
        t = (l + " " + m).strip()
        if pdfmetrics.stringWidth(t, font, size) > maxw and l: lignes.append(l); l = m
        else: l = t
    return lignes + [l]


# ------------------------------------------------------------------ tableau QSO
COLS = [("DATE", 0, "l"), ("UTC", 70, "l"), ("BANDE", 112, "l"), ("FRÉQ. MHz", 160, "l"),
        ("MODE", 236, "c"), ("RST ENV.", 300, "c"), ("RST REÇU", 360, "c")]

def entete_tableau(c, x0, y, tw, hh=16):
    c.setFillColor(NAVY); c.roundRect(x0, y, tw, hh, 3, stroke=0, fill=1)
    c.setFillColor(white); c.setFont("PopSemiBold", 7)
    for lab, dx, al in COLS:
        (c.drawCentredString if al == "c" else c.drawString)(x0 + 6 + dx, y + 5, lab)

def ligne_qso(c, x0, y, tw, q, i, rh=13.5):
    if i % 2: c.setFillColor(HexColor("#F2F3F6")); c.rect(x0, y, tw, rh, stroke=0, fill=1)
    freq = q.get("freq_mhz")
    vals = [fmt_date(q["date"]), q["heure_utc"], q["bande"],
            fmt_freq(float(freq)) if freq else "—",
            None, q.get("rst_envoye", ""), q.get("rst_recu", "")]
    for (lab, dx, al), v in zip(COLS, vals):
        x = x0 + 6 + dx
        if v is None:
            c.setFillColor(HexColor(MODE_COL.get(q["mode"], "#5A6478")))
            c.roundRect(x - 15, y + 2, 30, 10, 5, stroke=0, fill=1)
            c.setFillColor(white); c.setFont("PopBold", 6.5); c.drawCentredString(x, y + 4.3, q["mode"])
            continue
        c.setFillColor(NAVY2); c.setFont("PopBold" if lab == "BANDE" else "PopMedium", 8)
        (c.drawCentredString if al == "c" else c.drawString)(x, y + 4, str(v))

def tableau_p1(c, d, ytop):
    qso = d["qso"]; rh, hh = 13.5, 16
    deborde = len(qso) > MAX_LIGNES_P1
    visibles = qso[:MAX_LIGNES_P1 - 1] if deborde else qso
    y = ytop - hh; entete_tableau(c, X0, y, BW, hh)
    for i, q in enumerate(visibles):
        y -= rh; ligne_qso(c, X0, y, BW, q, i, rh)
    if deborde:
        y -= rh
        c.setFillColor(NAVY3); c.setFont("PopMedium", 7.5)
        c.drawCentredString(X0 + BW / 2, y + 4,
                            f"… et {len(qso) - len(visibles)} autres QSO — journal complet en annexe")
    c.setStrokeColor(GOLD); c.setLineWidth(1.2); c.line(X0, y, X0 + BW, y)
    s = d["stats"]
    parts = [("QSO ", str(s["nb"])), ("   BANDES ", str(s["bandes"])),
             ("   MODES ", " / ".join(s["modes"])), ("   POINTS ", str(s["points"]))]
    y -= 18; x = X0
    for k, v in parts:
        c.setFont("PopMedium", 8); c.setFillColor(GRIS); c.drawString(x, y, k)
        x += pdfmetrics.stringWidth(k, "PopMedium", 8)
        c.setFont("PopExtraBold", 11); c.setFillColor(RED2 if "POINTS" in k else NAVY2); c.drawString(x, y, v)
        x += pdfmetrics.stringWidth(v, "PopExtraBold", 11)
    return deborde


# ------------------------------------------------------------------ pages
def page_principale(c, d):
    act, dest = d["activation"], d["destinataire"]
    cls, cert = d.get("classement") or {}, d["certificat"]
    decor(c, d.get("logo_path"))
    medaille(c, cls.get("position"), cls.get("total"), W - 115, H - 110)

    c.setFillColor(NAVY); c.setFont("PopExtraBold", 60)
    c.drawString(X0, H - 150, act.get("titre", "CERTIFICAT"))
    st = act.get("sous_titre") or f"ACTIVATION SPÉCIALE · {act['evenement'].upper()}"
    c.setFont("PopSemiBold", fit(st, "PopSemiBold", 20, 400, 12)); c.drawString(X0 + 2, H - 180, st)

    c.setFillColor(NAVY); c.rect(X0, H - 222, BW, 24, stroke=0, fill=1)
    c.setFillColor(white); c.setFont("PopSemiBold", 10)
    c.drawCentredString(X0 + BW / 2, H - 214, "CE CERTIFICAT EST DÉCERNÉ À")

    nom = (dest.get("nom") or "").strip()
    if nom:
        c.setFillColor(NAVY2); c.setFont("Script", fit(nom, "Script", 44, BW - 20, 26))
        c.drawCentredString(X0 + BW / 2, H - 276, nom)
        ind, li = dest["indicatif"] + "   ", dest.get("locator", "")
    else:   # pas de nom : l'indicatif prend la place du nom
        c.setFillColor(NAVY2); c.setFont("PopExtraBold", 40)
        c.drawCentredString(X0 + BW / 2, H - 278, dest["indicatif"])
        ind, li = "", dest.get("locator", "")
    c.setStrokeColor(NAVY3); c.setLineWidth(1); c.line(X0 + 10, H - 290, X0 + BW - 10, H - 290)
    w1 = pdfmetrics.stringWidth(ind, "PopExtraBold", 20); w2 = pdfmetrics.stringWidth(li, "PopSemiBold", 10.5)
    xs = X0 + BW / 2 - (w1 + w2) / 2
    c.setFont("PopExtraBold", 20); c.setFillColor(RED2); c.drawString(xs, H - 311, ind)
    c.setFont("PopSemiBold", 10.5); c.setFillColor(NAVY); c.drawString(xs + w1, H - 311, li)

    texte = d.get("texte") or (f"pour avoir contacté la station spéciale {act['indicatif']}, "
                               f"activée lors du {act['evenement']} {act.get('periode', '')}.").replace(" .", ".")
    c.setFont("PopRegular", 9.5); c.setFillColor(GRIS); yy = H - 328
    for l in wrap(texte, "PopRegular", 9.5, BW)[:2]:
        c.drawCentredString(X0 + BW / 2, yy, l); yy -= 12.5
    deborde = tableau_p1(c, d, yy - 4)

    champs = [(X0 + 40, cert.get("gestionnaire", act["indicatif"]), "GESTIONNAIRE"),
              (585, cert.get("numero", ""), "N° DU CERTIFICAT"),
              (735, fmt_date(cert["date_emission"]), "DATE")]
    for x, v, lab in champs:
        c.setFillColor(NAVY2); c.setFont("PopSemiBold", fit(v, "PopSemiBold", 10.5, 124, 7))
        c.drawCentredString(x, 56, v)
        c.setStrokeColor(NAVY2); c.setLineWidth(0.8); c.line(x - 62, 50, x + 62, 50)
        c.setFont("PopMedium", 8.5); c.drawCentredString(x, 37, lab)
    if d.get("mention"):
        c.setFont("PopRegular", 6); c.setFillColor(HexColor("#8A8F99")); c.drawString(X0, 14, d["mention"])
    c.showPage()
    return deborde

def pages_annexe(c, d):
    act, dest, qso = d["activation"], d["destinataire"], d["qso"]
    tw, x0 = 560, (W - 560) / 2
    pages = [qso[i:i + LIGNES_ANNEXE] for i in range(0, len(qso), LIGNES_ANNEXE)]
    for n, bloc in enumerate(pages, 1):
        c.setFillColor(white); c.rect(0, 0, W, H, stroke=0, fill=1)
        c.setFillColor(NAVY); c.rect(0, H - 70, W, 70, stroke=0, fill=1)
        c.setFillColor(GOLD); c.rect(0, H - 76, W, 6, stroke=0, fill=1)
        c.setFillColor(white); c.setFont("PopExtraBold", 20)
        c.drawString(40, H - 45, f"JOURNAL DES CONTACTS · {dest['indicatif']}")
        c.setFont("PopMedium", 10)
        c.drawRightString(W - 40, H - 45, f"{act['indicatif']} · {act['evenement']} · annexe {n}/{len(pages)}")
        tw2 = 420; x0 = (W - tw2) / 2
        y = H - 110; entete_tableau(c, x0, y, tw2)
        for i, q in enumerate(bloc):
            y -= 14.5; ligne_qso(c, x0, y, tw2, q, i, 14.5)
        c.setStrokeColor(GOLD); c.setLineWidth(1.2); c.line(x0, y, x0 + tw2, y)
        s = d["stats"]
        c.setFont("PopMedium", 8); c.setFillColor(GRIS)
        c.drawCentredString(W / 2, 30, f"{s['nb']} QSO · {s['bandes']} bandes · "
                                       f"{' / '.join(s['modes'])} · {s['points']} points · "
                                       f"certificat n° {d['certificat'].get('numero', '')}")
        c.showPage()

def render(data: dict) -> bytes:
    register_fonts()
    d = normaliser(data)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Certificat {d['activation']['indicatif']} — {d['destinataire']['indicatif']}")
    c.setAuthor(d["certificat"].get("gestionnaire", d["activation"]["indicatif"]))
    if page_principale(c, d):
        pages_annexe(c, d)
    c.save()
    return buf.getvalue()
