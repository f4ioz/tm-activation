"""DYNAMIC layer: fills in the certificate from a dict described in SPEC.md.
API:  render(data: dict) -> bytes (PDF)

The printed wording is French for now; only the code is in English."""

import io
from datetime import date, datetime
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from .decor import (
    register_fonts,
    decor,
    medal,
    border,
    flag,
    emblem,
    ham_symbol,
    W,
    H,
    A,
    NAVY,
    NAVY2,
    NAVY3,
    GOLD,
    GOLDL,
    RED2,
    GREY,
)

MAX_ROWS_P1 = 10  # QSO rows shown on page 1 (default; see options)


def options(d):
    """Layout options: ``max_qso`` (rows on page 1) and ``appendix``."""
    o = d.get("options") or {}
    try:
        max_rows = int(o.get("max_qso", MAX_ROWS_P1))
    except (TypeError, ValueError):
        max_rows = MAX_ROWS_P1
    return {"max_qso": max(1, min(max_rows, 14)), "appendix": bool(o.get("appendix", True))}


APPENDIX_ROWS = 28  # QSO rows per appendix page
X0, BW = 228, 420  # content column
CAP = 0.70  # Poppins cap height, as a fraction of the font size
TITLE_MAXW = 655 - X0  # title + flag stop before the medal
MODE_COL = {
    "SSB": "#C9982E",
    "CW": "#B0202B",
    "FT8": "#2C6E9C",
    "FT4": "#2C6E9C",
    "FM": "#3B7D4F",
    "RTTY": "#6A4C93",
    "DIGI": "#2C6E9C",
}
BANDS = [
    (1.8, 2.0, "160 m"),
    (3.5, 3.8, "80 m"),
    (5.3, 5.4, "60 m"),
    (7.0, 7.2, "40 m"),
    (10.1, 10.15, "30 m"),
    (14.0, 14.35, "20 m"),
    (18.068, 18.168, "17 m"),
    (21.0, 21.45, "15 m"),
    (24.89, 24.99, "12 m"),
    (28.0, 29.7, "10 m"),
    (50, 52, "6 m"),
    (144, 146, "2 m"),
    (430, 440, "70 cm"),
    (1240, 1300, "23 cm"),
    (2400, 2450, "13 cm"),
    (10000, 10500, "3 cm"),
]


# ------------------------------------------------------------------ helpers
def band_of(freq):
    for lo, hi, b in BANDS:
        if lo <= freq <= hi:
            return b
    return "?"


def fmt_freq(f):
    return f"{f:,.3f}".replace(",", " ").replace(".", ",")


def fmt_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return s or ""


def normalize(data):
    """Completes / cleans the data: band, chronological order, points, totals."""
    d = {k: (dict(v) if isinstance(v, dict) else v) for k, v in data.items()}
    scoring = {"*": 1, **(d.get("scoring") or {})}
    qso = []
    for q in d.get("qso", []):
        q = dict(q)
        q["mode"] = q["mode"].upper()
        freq = q.get("freq_mhz")
        q["band"] = q.get("band") or (band_of(float(freq)) if freq else "?")
        # The caller may set a QSO's points (house scoring, duplicates counted
        # as zero…); otherwise the per-mode scoring applies.
        q["points"] = q.get("points", scoring.get(q["mode"], scoring["*"]))
        qso.append(q)
    qso.sort(key=lambda q: (q["date"], q["time_utc"]))
    d["qso"] = qso
    d["stats"] = dict(
        count=len(qso),
        bands=len({q["band"] for q in qso}),
        modes=sorted({q["mode"] for q in qso}),
        points=sum(q["points"] for q in qso),
    )
    cert = d.setdefault("certificate", {})
    cert.setdefault("issue_date", date.today().isoformat())
    return d


def fit(txt, font, size, maxw, mini):
    while size > mini and pdfmetrics.stringWidth(txt, font, size) > maxw:
        size -= 1
    return size


def wrap(txt, font, size, maxw):
    lines, cur = [], ""
    for word in txt.split():
        t = (cur + " " + word).strip()
        if pdfmetrics.stringWidth(t, font, size) > maxw and cur:
            lines.append(cur)
            cur = word
        else:
            cur = t
    return lines + [cur]


# ------------------------------------------------------------------ QSO table
COLS = [
    ("DATE", 0, "l"),
    ("UTC", 70, "l"),
    ("BANDE", 112, "l"),
    ("FRÉQ. MHz", 160, "l"),
    ("MODE", 236, "c"),
    ("RST ENV.", 300, "c"),
    ("RST REÇU", 360, "c"),
]


def table_header(c, x0, y, tw, hh=16):
    c.setFillColor(NAVY)
    c.roundRect(x0, y, tw, hh, 3, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("PopSemiBold", 7)
    for lab, dx, al in COLS:
        (c.drawCentredString if al == "c" else c.drawString)(x0 + 6 + dx, y + 5, lab)


def qso_row(c, x0, y, tw, q, i, rh=13.5):
    if i % 2:
        c.setFillColor(HexColor("#F2F3F6"))
        c.rect(x0, y, tw, rh, stroke=0, fill=1)
    freq = q.get("freq_mhz")
    vals = [
        fmt_date(q["date"]),
        q["time_utc"],
        q["band"],
        fmt_freq(float(freq)) if freq else "—",
        None,
        q.get("rst_sent", ""),
        q.get("rst_rcvd", ""),
    ]
    for (lab, dx, al), v in zip(COLS, vals):
        x = x0 + 6 + dx
        if v is None:
            c.setFillColor(HexColor(MODE_COL.get(q["mode"], "#5A6478")))
            c.roundRect(x - 15, y + 2, 30, 10, 5, stroke=0, fill=1)
            c.setFillColor(white)
            c.setFont("PopBold", 6.5)
            c.drawCentredString(x, y + 4.3, q["mode"])
            continue
        c.setFillColor(NAVY2)
        c.setFont("PopBold" if lab == "BANDE" else "PopMedium", 8)
        (c.drawCentredString if al == "c" else c.drawString)(x, y + 4, str(v))


def table_p1(c, d, ytop):
    qso = d["qso"]
    rh, hh = 13.5, 16
    opt = options(d)
    max_rows = opt["max_qso"]
    overflow = len(qso) > max_rows
    shown = qso[: max_rows - 1] if overflow else qso
    y = ytop - hh
    table_header(c, X0, y, BW, hh)
    for i, q in enumerate(shown):
        y -= rh
        qso_row(c, X0, y, BW, q, i, rh)
    if overflow:
        y -= rh
        rest = len(qso) - len(shown)
        more = " — journal complet en annexe" if opt["appendix"] else ""
        c.setFillColor(NAVY3)
        c.setFont("PopMedium", 7.5)
        c.drawCentredString(X0 + BW / 2, y + 4, f"… et {rest} autres QSO{more}")
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.2)
    c.line(X0, y, X0 + BW, y)
    s = d["stats"]
    parts = [
        ("QSO ", str(s["count"])),
        ("   BANDES ", str(s["bands"])),
        ("   MODES ", " / ".join(s["modes"])),
        ("   POINTS ", str(s["points"])),
    ]
    y -= 18
    x = X0
    for k, v in parts:
        c.setFont("PopMedium", 8)
        c.setFillColor(GREY)
        c.drawString(x, y, k)
        x += pdfmetrics.stringWidth(k, "PopMedium", 8)
        c.setFont("PopExtraBold", 11)
        c.setFillColor(RED2 if "POINTS" in k else NAVY2)
        c.drawString(x, y, v)
        x += pdfmetrics.stringWidth(v, "PopExtraBold", 11)
    return overflow


MORSE = {
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    "0": "-----",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
    "/": "-..-.",
}


def morse_rule(c, x, y, text, width, thickness=3.2, col=GOLD):
    """Underlines a text with its Morse code (dots and dashes).

    The unit is computed so that the line is exactly ``width`` long: the
    underline follows the title, whatever the callsign."""
    letters = [MORSE.get(ch.upper(), "") for ch in (text or "") if ch.strip()]
    letters = [m for m in letters if m]
    if not letters:
        return
    # Width in units: dot 1, dash 3, gap 1, space between letters 3.
    units = sum(sum(3 if s == "-" else 1 for s in m) + (len(m) - 1) for m in letters)
    units += 3 * (len(letters) - 1)
    u = width / max(units, 1)
    c.setFillColor(col)
    cx = x
    for i, m in enumerate(letters):
        for j, sign in enumerate(m):
            w = 3 * u if sign == "-" else u
            c.roundRect(cx, y, w, thickness, thickness / 2, stroke=0, fill=1)
            cx += w + (u if j < len(m) - 1 else 0)
        if i < len(letters) - 1:
            cx += 3 * u


# ------------------------------------------------------------------ pages
def qr_tile(c, url, cx=65, cy=162, side=66):
    """QR code on a white tile, in the navy column below the logo; the address
    (without "https://") in small print underneath, for those who don't scan."""
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing

    w = qr.QrCodeWidget(url, barLevel="M", barBorder=0)
    x0, y0, x1, y1 = w.getBounds()
    k = side / max(x1 - x0, y1 - y0)
    dr = Drawing(side, side, transform=[k, 0, 0, k, 0, 0])
    dr.add(w)
    pad = 5
    c.setFillColor(A(HexColor("#000000"), 0.25))
    c.roundRect(
        cx - side / 2 - pad + 2, cy - side / 2 - pad - 2, side + 2 * pad, side + 2 * pad, 5, stroke=0, fill=1
    )
    c.setFillColor(white)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.2)
    c.roundRect(cx - side / 2 - pad, cy - side / 2 - pad, side + 2 * pad, side + 2 * pad, 5, stroke=1, fill=1)
    renderPDF.draw(dr, c, cx - side / 2, cy - side / 2)
    short = url.split("://", 1)[-1].rstrip("/")
    c.setFillColor(GOLDL)
    c.setFont("PopMedium", fit(short, "PopMedium", 6.5, side + 30, 4.5))
    c.drawCentredString(cx, cy - side / 2 - pad - 9, short)


def main_page(c, d):
    act, rcpt = d["activation"], d["recipient"]
    rank, cert = d.get("ranking") or {}, d["certificate"]
    decor(c, d.get("logo_path"))
    if d.get("border"):
        border(c, d["border"])
    medal(c, rank.get("position"), rank.get("total"), W - 115, H - 110)
    # Below the radio set: the emblem (banner with the club name, otherwise
    # "HAM RADIO") and/or the amateur radio symbol, each one optional.
    emb, sym = d.get("emblem"), d.get("ham_symbol")
    if emb is not None:
        emblem(c, 733 if sym else 745, 176, emb.get("text") or "HAM RADIO")
    if sym:
        ham_symbol(
            c, 808 if emb is not None else 745, 205 if emb is not None else 205, 46 if emb is not None else 70
        )

    title = act.get("title") or "CERTIFICAT"
    # Country flag to the RIGHT of the title, on its line and at the height of
    # its capitals; the title shrinks if both would run into the medal.
    ratio = None
    if d.get("flag"):
        try:
            from PIL import Image

            with Image.open(d["flag"]) as im:
                ratio = im.width / im.height
        except OSError:
            ratio = None
    fs = 60.0

    def total_width(f):
        w = pdfmetrics.stringWidth(title, "PopExtraBold", f)
        return w + (14 + CAP * f * ratio if ratio else 0)

    while fs > 36 and total_width(fs) > TITLE_MAXW:
        fs -= 1
    yt = H - 140
    c.setFillColor(NAVY)
    c.setFont("PopExtraBold", fs)
    c.drawString(X0, yt, title)
    wt = pdfmetrics.stringWidth(title, "PopExtraBold", fs)
    # Below the title, the same in Morse code: a nod to CW, and a visual cue.
    morse_rule(c, X0, yt - 10, act.get("morse") or title, wt)
    if ratio:
        try:
            flag(c, d["flag"], X0 + wt + 14, yt + CAP * fs / 2, CAP * fs)
        except OSError:
            pass
    # Subtitle: the one given; an empty string removes it altogether.
    st = act["subtitle"] if "subtitle" in act else f"ACTIVATION SPÉCIALE · {act['event'].upper()}"
    if st:
        c.setFillColor(NAVY)  # morse_rule left gold in the brush
        c.setFont("PopSemiBold", fit(st, "PopSemiBold", 20, 400, 12))
        c.drawString(X0 + 2, H - 176, st)
    if d.get("qr_url"):
        qr_tile(c, d["qr_url"])

    c.setFillColor(NAVY)
    c.rect(X0, H - 222, BW, 24, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("PopSemiBold", 10)
    c.drawCentredString(X0 + BW / 2, H - 214, "CE CERTIFICAT EST DÉCERNÉ À")

    name = (rcpt.get("name") or "").strip()
    if name:
        # The name must be READABLE: foreign capitals, accents, callsigns glued
        # to the name… a script font looked nice but was hard to decipher.
        c.setFillColor(NAVY2)
        c.setFont("PopSemiBold", fit(name, "PopSemiBold", 30, BW - 20, 17))
        c.drawCentredString(X0 + BW / 2, H - 274, name)
        call, loc = rcpt["callsign"] + "   ", rcpt.get("locator", "")
    else:  # no name: the callsign takes the name's place
        c.setFillColor(NAVY2)
        c.setFont("PopExtraBold", 44)
        c.drawCentredString(X0 + BW / 2, H - 278, rcpt["callsign"])
        call, loc = "", rcpt.get("locator", "")
    c.setStrokeColor(NAVY3)
    c.setLineWidth(1)
    c.line(X0 + 10, H - 290, X0 + BW - 10, H - 290)
    # The recipient's callsign is what one reads first on a wall: it is larger
    # than the rest, the locator staying discreet beside it.
    fs = fit(call + loc, "PopExtraBold", 30, BW - 40, 18)
    w1 = pdfmetrics.stringWidth(call, "PopExtraBold", fs)
    w2 = pdfmetrics.stringWidth(loc, "PopSemiBold", fs * 0.42)
    xs = X0 + BW / 2 - (w1 + w2) / 2
    c.setFont("PopExtraBold", fs)
    c.setFillColor(RED2)
    c.drawString(xs, H - 318, call)
    c.setFont("PopSemiBold", fs * 0.42)
    c.setFillColor(NAVY)
    c.drawString(xs + w1, H - 318, loc)

    text = d.get("award_text") or (
        f"pour avoir contacté la station spéciale {act['callsign']}, "
        f"activée lors du {act['event']} {act.get('period', '')}."
    ).replace(" .", ".")
    c.setFont("PopRegular", 9.5)
    c.setFillColor(GREY)
    yy = H - 336
    for line in wrap(text, "PopRegular", 9.5, BW)[:2]:
        c.drawCentredString(X0 + BW / 2, yy, line)
        yy -= 12.5
    overflow = table_p1(c, d, yy - 4)

    fields = [
        (X0 + 40, cert.get("manager", act["callsign"]), "GESTIONNAIRE"),
        (585, cert.get("number", ""), "N° DU CERTIFICAT"),
        (735, fmt_date(cert["issue_date"]), "DATE"),
    ]
    for x, v, lab in fields:
        c.setFillColor(NAVY2)
        c.setFont("PopSemiBold", fit(v, "PopSemiBold", 10.5, 124, 7))
        c.drawCentredString(x, 56, v)
        c.setStrokeColor(NAVY2)
        c.setLineWidth(0.8)
        c.line(x - 62, 50, x + 62, 50)
        c.setFont("PopMedium", 8.5)
        c.drawCentredString(x, 37, lab)
    if d.get("footnote"):
        c.setFont("PopRegular", 6)
        c.setFillColor(HexColor("#8A8F99"))
        c.drawString(X0, 14, d["footnote"])
    c.showPage()
    return overflow


def appendix_pages(c, d):
    act, rcpt, qso = d["activation"], d["recipient"], d["qso"]
    pages = [qso[i : i + APPENDIX_ROWS] for i in range(0, len(qso), APPENDIX_ROWS)]
    for n, chunk in enumerate(pages, 1):
        c.setFillColor(white)
        c.rect(0, 0, W, H, stroke=0, fill=1)
        c.setFillColor(NAVY)
        c.rect(0, H - 70, W, 70, stroke=0, fill=1)
        c.setFillColor(GOLD)
        c.rect(0, H - 76, W, 6, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("PopExtraBold", 20)
        c.drawString(40, H - 45, f"JOURNAL DES CONTACTS · {rcpt['callsign']}")
        c.setFont("PopMedium", 10)
        c.drawRightString(W - 40, H - 45, f"{act['callsign']} · {act['event']} · annexe {n}/{len(pages)}")
        tw = 420
        x0 = (W - tw) / 2
        y = H - 110
        table_header(c, x0, y, tw)
        for i, q in enumerate(chunk):
            y -= 14.5
            qso_row(c, x0, y, tw, q, i, 14.5)
        c.setStrokeColor(GOLD)
        c.setLineWidth(1.2)
        c.line(x0, y, x0 + tw, y)
        s = d["stats"]
        c.setFont("PopMedium", 8)
        c.setFillColor(GREY)
        c.drawCentredString(
            W / 2,
            30,
            f"{s['count']} QSO · {s['bands']} bandes · "
            f"{' / '.join(s['modes'])} · {s['points']} points · "
            f"certificat n° {d['certificate'].get('number', '')}",
        )
        c.showPage()


def render(data: dict) -> bytes:
    register_fonts()
    d = normalize(data)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Certificat {d['activation']['callsign']} — {d['recipient']['callsign']}")
    c.setAuthor(d["certificate"].get("manager", d["activation"]["callsign"]))
    if main_page(c, d) and options(d)["appendix"]:
        appendix_pages(c, d)
    c.save()
    return buf.getvalue()
