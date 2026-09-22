"""Rapport PDF d'une activation : bilan illustré, pour l'administrateur.

Une page A4 (deux si le palmarès est long) reprenant ce qui compte : le titre
de l'activation, les compteurs, le rythme (par jour et par heure), la
répartition par bande et par mode, les entités DXCC et les meilleurs chasseurs.

Dessiné avec :mod:`app.pdf` — aucune dépendance extérieure, donc un Pi hors
ligne sort le même document qu'un serveur.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import activation, dxcc_flags, pdf
from app.config import club_config
from app.i18n import gettext as _

# ── Palette (reprise du site) ─────────────────────────────────────────────
INK = pdf.hex_color("#101623")
MUTED = pdf.hex_color("#6b7684")
LINE = pdf.hex_color("#dde3ec")
PANEL = pdf.hex_color("#f5f7fb")
ACCENT = pdf.hex_color("#1e5fbf")
ACCENT_DARK = pdf.hex_color("#0d3f8f")
WHITE = (1.0, 1.0, 1.0)
BAND_COLOR = pdf.hex_color("#2f7fd1")
KPI_COLORS = ["#1e5fbf", "#0f9d8f", "#c2632a", "#7048a8", "#2f8f4e"]

MARGIN = 38.0
FOOT_ROOM = 48.0      # place réservée au pied de page
SIGNATURE = "TM-Activation · F4IOZ"

# Trois densités : on compose au large, et si le document déborde de peu (ou si
# « une seule page » est demandé), on recommence en resserrant — d'abord les
# barres du rythme et les interlignes, puis le bandeau de titre.
DENSITIES: list[dict[str, float]] = [
    {"band": 104, "kpi": 54, "day": 118, "day_hours": 92, "hour": 74,
     "grid": 15.0, "row": 14.0, "gap": 14.0, "title": 30, "kpi_value": 21,
     "label": 12.5, "meta": 9.5},
    {"band": 88, "kpi": 46, "day": 88, "day_hours": 68, "hour": 58,
     "grid": 13.5, "row": 12.5, "gap": 9.0, "title": 26, "kpi_value": 18,
     "label": 11.5, "meta": 9.0},
    {"band": 74, "kpi": 40, "day": 66, "day_hours": 52, "hour": 46,
     "grid": 12.5, "row": 11.5, "gap": 6.0, "title": 22, "kpi_value": 16,
     "label": 10.5, "meta": 8.5},
]
FLAGS_DIR = Path(__file__).resolve().parent.parent / "static" / "vendor" / "flags"


def _date_fr(day: str) -> str:
    """« 20260911 » → « 11/09 »."""
    return f"{day[6:8]}/{day[4:6]}" if len(day) == 8 else day


def _period(station: dict[str, Any], timeline: dict[str, Any]) -> str:
    """Dates de l'activation : celles de la fiche, sinon celles du log."""
    start, end = (station.get("start_date") or ""), (station.get("end_date") or "")
    if start and end:
        return _("Du {start} au {end}", start=_iso_fr(start), end=_iso_fr(end))
    days = timeline["by_day"]
    if not days:
        return ""
    return _("Du {start} au {end}", start=_date_fr(days[0]["day"]), end=_date_fr(days[-1]["day"]))


def _iso_fr(iso: str) -> str:
    parts = (iso or "").split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else iso


def sat_icon(page: pdf.Page, x: float, y: float, size: float, color: pdf.Color) -> None:
    """Petit satellite vectoriel : deux panneaux, un corps, une antenne.

    Helvetica n'a pas de symbole satellite et on n'embarque pas de police
    supplémentaire : quelques rectangles suffisent, et restent nets à
    l'impression comme à l'écran."""
    u = size / 14.0                      # le dessin est pensé sur une grille de 14
    panel = pdf.mix(WHITE, color, 0.55)
    body_w, body_h = 4 * u, 7 * u
    cx = x + size / 2
    top = y + 3 * u                      # 3 unités réservées à l'antenne
    # Panneaux solaires de part et d'autre, avec leurs cellules.
    for left in (cx - body_w / 2 - 1 * u - 4 * u, cx + body_w / 2 + 1 * u):
        page.rect(left, top + 1 * u, 4 * u, 5 * u, fill=panel)
        for k in (1, 2, 3):
            page.line(left + k * u, top + 1 * u, left + k * u, top + 6 * u, color=WHITE, width=0.4)
    # Corps.
    page.rect(cx - body_w / 2, top, body_w, body_h, fill=color, radius=0.8 * u)
    # Antenne et son petit réflecteur.
    page.line(cx, top, cx, y + 0.8 * u, color=color, width=0.9)
    page.line(cx - 1.6 * u, y + 0.9 * u, cx + 1.6 * u, y + 0.9 * u, color=color, width=0.9)


def flag_bytes(code: str) -> bytes | None:
    """Vignette PNG d'une entité DXCC (None si elle manque)."""
    if not code:
        return None
    path = FLAGS_DIR / f"{dxcc_flags.flag_file(code)}.png"
    try:
        return path.read_bytes()
    except OSError:
        return None


class _Sheet:
    """Une page du rapport, avec un curseur vertical et les blocs de dessin."""

    def __init__(self, doc: pdf.Pdf, m: dict[str, float]) -> None:
        self.page = doc.page()
        self.m = m
        self.y = MARGIN
        self.width = self.page.width - 2 * MARGIN

    # ── briques ───────────────────────────────────────────────────────────
    def title_band(self, callsign: str, label: str, period: str, club: str,
                   logo: bytes | None = None) -> None:
        page = self.page
        band_h = self.m["band"]
        page.rect(0, 0, page.width, band_h, fill=ACCENT)
        page.rect(0, band_h - 6, page.width, 6, fill=ACCENT_DARK)
        text_left = MARGIN
        if logo:
            # Le logo occupe la gauche du bandeau, à hauteur fixe et sans
            # déformation ; le titre se décale d'autant.
            try:
                # Dimensions lues dans l'en-tête (PNG comme JPEG) : décoder
                # l'image entière juste pour ses proportions coûterait cher, et
                # le rapport est composé plusieurs fois.
                lw, lh = pdf.image_size(logo)
            except (ValueError, IndexError):
                lw = lh = 0
            if lw and lh:
                # Presque toute la hauteur du bandeau, sans cadre : le PNG est
                # détouré, sa transparence est conservée par le masque PDF.
                box_h = band_h - 22
                box_w = min(box_h * lw / lh, 170.0)
                page.image(MARGIN, (band_h - box_h) / 2, box_w, box_h, logo, max_side=340)
                text_left = MARGIN + box_w + 16
        # Les lignes sont EMPILÉES et le bloc centré : le bandeau change de
        # hauteur selon la densité, des positions en dur finissaient par se
        # chevaucher (vu sur un rapport resserré à une page).
        title_size, label_size, meta_size = self.m["title"], self.m["label"], self.m["meta"]
        stamp = datetime.now(UTC).strftime("%d/%m/%Y %H:%M")
        line = " · ".join(bit for bit in (period, club) if bit)
        left_lines = [(callsign, title_size, True, WHITE)]
        if label:
            left_lines.append((label, label_size, False, WHITE))
        if line:
            left_lines.append((line, meta_size, False, pdf.mix(WHITE, ACCENT, 0.35)))
        right_lines = [(_("Rapport d'activité"), meta_size + 1.5, True, WHITE),
                       (_("établi le {when} UTC", when=stamp), meta_size,
                        False, pdf.mix(WHITE, ACCENT, 0.35))]

        def draw(lines: list[tuple[str, float, bool, pdf.Color]], x: float, width: float,
                 align: str) -> None:
            spacing = 3.0 if self.m["gap"] > 8 else 2.0
            height = sum(size * 1.15 for _text, size, _b, _c in lines) + spacing * (len(lines) - 1)
            y = max((band_h - 6 - height) / 2, 4.0)
            for text, size, bold, color in lines:
                page.text(x, y, text, size=size, bold=bold, color=color,
                          align=align, width=width)
                y += size * 1.15 + spacing

        right_width = self.width * 0.30
        draw(left_lines, text_left, MARGIN + self.width - right_width - 12 - text_left, "left")
        draw(right_lines, MARGIN + self.width - right_width, right_width, "right")
        self.y = band_h + self.m["gap"] + 10

    def section(self, title: str, hint: str = "", icon: str = "") -> None:
        left = MARGIN
        if icon == "sat":
            sat_icon(self.page, MARGIN, self.y - 1.5, 14, ACCENT)
            left = MARGIN + 19
        self.page.text(left, self.y, title.upper(), size=10.5, bold=True, color=ACCENT)
        if hint:
            self.page.text(MARGIN, self.y + 1, hint, size=8.5, color=MUTED,
                           align="right", width=self.width)
        self.y += 15
        self.page.line(MARGIN, self.y, MARGIN + self.width, self.y, color=LINE, width=0.8)
        self.y += min(12.0, self.m["gap"])

    def kpis(self, items: list[tuple[str, str]]) -> None:
        """Bandeau de compteurs : un pavé coloré par chiffre clé."""
        gap = 9.0
        count = max(len(items), 1)
        w = (self.width - gap * (count - 1)) / count
        for i, (value, label) in enumerate(items):
            color = pdf.hex_color(KPI_COLORS[i % len(KPI_COLORS)])
            x = MARGIN + i * (w + gap)
            h = self.m["kpi"]
            self.page.rect(x, self.y, w, h, fill=pdf.mix(WHITE, color, 0.10),
                           stroke=pdf.mix(WHITE, color, 0.35), radius=5)
            self.page.rect(x, self.y, 3.5, h, fill=color)
            self.page.text(x + 10, self.y + h * 0.16, value, size=self.m["kpi_value"], bold=True,
                           color=color, width=w - 16)
            self.page.text(x + 10, self.y + h - 18, label.upper(), size=7.5, color=MUTED,
                           width=w - 16)
        self.y += self.m["kpi"] + self.m["gap"] + 4

    def columns(self, values: list[float], labels: list[str], height: float,
                color: pdf.Color, every: int = 1, value_labels: bool = True) -> None:
        """Histogramme en colonnes (rythme)."""
        top = self.y
        peak = max(values) if values and max(values) > 0 else 1
        n = max(len(values), 1)
        gap = 3.0 if n <= 32 else 1.6
        w = (self.width - gap * (n - 1)) / n
        base = top + height
        self.page.line(MARGIN, base, MARGIN + self.width, base, color=LINE, width=0.8)
        for i, value in enumerate(values):
            x = MARGIN + i * (w + gap)
            bar = (value / peak) * (height - 14) if value else 0
            if bar > 0:
                self.page.rect(x, base - bar, w, bar,
                               fill=pdf.mix(WHITE, color, 0.35 + 0.65 * (value / peak)), radius=1.5)
            if value_labels and value:
                self.page.text(x, base - bar - 10, str(int(value)), size=6.5, color=MUTED,
                               align="center", width=w)
            if i % every == 0:
                self.page.text(x, base + 3, labels[i], size=6.5, color=MUTED,
                               align="center", width=w)
        self.y = base + 16

    def bars(self, x: float, width: float, rows: list[tuple[str, int, pdf.Color]],
             total: int) -> float:
        """Barres horizontales « libellé | barre | compte » (bandes, modes)."""
        y = self.y
        for label, value, color in rows:
            self.page.text(x, y, label, size=9, bold=True, color=INK, width=52)
            track = width - 52 - 42
            self.page.rect(x + 52, y + 1, track, 9, fill=PANEL, radius=2)
            share = (value / total) if total else 0
            if share > 0:
                self.page.rect(x + 52, y + 1, max(track * share, 2), 9, fill=color, radius=2)
            self.page.text(x + width - 42, y, str(value), size=9, color=INK,
                           align="right", width=42)
            y += 17
        return y

    def table(self, x: float, width: float, head: tuple[str, ...], rows: list[tuple[str, ...]],
              widths: tuple[float, ...]) -> float:
        """Petit tableau (entités DXCC, chasseurs)."""
        y = self.y
        cx = x
        for title, w in zip(head, widths):
            align = "left" if w == widths[0] else "right"
            self.page.text(cx, y, title.upper(), size=7.5, bold=True, color=MUTED,
                           align=align, width=w)
            cx += w
        y += 12
        self.page.line(x, y - 2, x + width, y - 2, color=LINE, width=0.7)
        for i, row in enumerate(rows):
            if i % 2 == 1:
                self.page.rect(x - 3, y - 2, width + 6, self.m["row"], fill=PANEL)
            cx = x
            for cell, w in zip(row, widths):
                align = "left" if w == widths[0] else "right"
                self.page.text(cx, y, cell, size=8.5, color=INK, align=align, width=w)
                cx += w
            y += self.m["row"]
        return y

    def flag_grid(self, entities: list[dict[str, Any]], columns: int = 3,
                  more: int = 0) -> None:
        """Toutes les entités contactées : drapeau, nom et nombre de QSO.

        Disposées en colonnes pour qu'une centaine d'entités tienne sans
        transformer le rapport en annuaire."""
        gap = 14.0
        col_w = (self.width - gap * (columns - 1)) / columns
        rows = (len(entities) + columns - 1) // columns
        line_h = self.m["grid"]
        for i, entity in enumerate(entities):
            col, row = i // rows, i % rows
            x = MARGIN + col * (col_w + gap)
            y = self.y + row * line_h
            if i % 2 == 0:
                self.page.rect(x - 3, y - 2, col_w + 6, line_h - 1, fill=PANEL)
            flag = flag_bytes(entity.get("code") or "")
            if flag:
                self.page.image(x, y, 15.0, 10.5, flag)
            self.page.text(x + 20, y, entity.get("dxcc_name") or "", size=8.5, color=INK,
                           width=col_w - 20 - 30)
            self.page.text(x + col_w - 30, y, str(entity.get("qsos") or 0), size=8.5,
                           bold=True, color=ACCENT, align="right", width=30)
        self.y += rows * line_h + 6
        if more:
            self.page.text(MARGIN, self.y, _("… et {n} autres entités", n=more), size=8,
                           color=MUTED)
            self.y += 12

    def room_left(self) -> float:
        """Hauteur disponible avant le pied de page."""
        return self.page.height - FOOT_ROOM - self.y

    @staticmethod
    def footer_on(page: pdf.Page, text: str, page_no: int, pages: int) -> None:
        """Pied de page, posé à la fin quand le nombre de pages est connu."""
        width = page.width - 2 * MARGIN
        y = page.height - 26
        page.line(MARGIN, y - 8, MARGIN + width, y - 8, color=LINE, width=0.7)
        page.text(MARGIN, y, text, size=7.5, color=MUTED, width=width * 0.45)
        page.text(MARGIN, y, SIGNATURE, size=7.5, color=MUTED, align="center", width=width)
        page.text(MARGIN, y, _("Page {n}/{total}", n=page_no, total=pages), size=7.5,
                  color=MUTED, align="right", width=width)


class _Book:
    """Suite de pages : ``room(n)`` renvoie une page où il reste la place voulue."""

    def __init__(self, doc: pdf.Pdf, m: dict[str, float]) -> None:
        self.doc = doc
        self.m = m
        self.sheet = _Sheet(doc, m)

    def new_sheet(self) -> _Sheet:
        self.sheet = _Sheet(self.doc, self.m)
        self.sheet.y = MARGIN + 4
        return self.sheet

    def room(self, needed: float) -> _Sheet:
        if self.sheet.room_left() < needed:
            return self.new_sheet()
        return self.sheet


def _gather(call: str) -> dict[str, Any]:
    """Toutes les données du rapport, lues une seule fois.

    La composition peut être rejouée à plusieurs densités pour tenir en une
    page : inutile d'interroger la base à chaque essai."""
    return {
        "station": activation.get_station(call),
        "stats": activation.stats(call),
        "timeline": activation.qso_timeline(call),
        "dxcc": activation.dxcc_table(call),
        "hunters": activation.hunters_ranking(None, call),
        "map_data": activation.map_data(call),
        "style": activation.get_map_style(call),
        "sats": activation.satellite_stats(call),
    }


def _compose(call: str, data: dict[str, Any], opts: dict[str, Any], m: dict[str, float],
             logo: bytes | None, club_line: str, single: bool) -> pdf.Pdf:
    """Compose le document à une densité donnée.

    ``single`` : on s'interdit la deuxième page — les listes sont coupées à ce
    qui tient, avec la mention du reste."""
    st, stats = data["station"], data["stats"]
    timeline, dxcc, hunters = data["timeline"], data["dxcc"], data["hunters"]
    doc = pdf.Pdf()
    doc.title = _("{call} — rapport d'activité", call=call)
    doc.author = club_line or call
    book = _Book(doc, m)
    sheet = book.sheet
    sheet.title_band(call, st.get("label") or "", _period(st, timeline), club_line, logo)

    sheet.kpis([
        (str(stats["total"]), _("QSO")),
        (str(dxcc["count"]), _("entités DXCC")),
        (str(len({h["call"] for h in hunters})), _("stations")),
        (str(len(stats["by_op"])), _("opérateurs")),
        (f"{timeline['per_hour']:g}", _("QSO/h en trafic")),
    ])

    # ── Rythme jour par jour (les barres rétrécissent quand on resserre) ──
    best_day = timeline["best_day"]
    hint = _("Meilleure journée : {day} ({n} QSO)",
             day=_date_fr(best_day["day"]), n=best_day["n"]) if best_day else ""
    sheet.section(_("Rythme, jour par jour"), hint)
    days = timeline["by_day"]
    if days:
        sheet.columns([d["n"] for d in days], [_date_fr(d["day"]) for d in days],
                      height=m["day_hours"] if opts["hours"] else m["day"], color=ACCENT,
                      every=max(1, len(days) // 14))
    else:
        sheet.page.text(MARGIN, sheet.y, _("Aucun QSO enregistré."), size=9, color=MUTED)
        sheet.y += 20

    if opts["hours"]:
        slot = timeline["best_slot"]
        hint = _("Heure la plus forte : {day} à {hour} h UTC ({n} QSO)",
                 day=_date_fr(slot["day"]), hour=slot["hour"], n=slot["n"]) if slot["n"] else ""
        sheet.y += 4
        sheet.section(_("Rythme, heure par heure (UTC)"), hint)
        sheet.columns(timeline["by_hour"], [f"{h:02d}" for h in range(24)],
                      height=m["hour"], color=pdf.hex_color("#0f9d8f"), every=2,
                      value_labels=False)
    sheet.page.text(MARGIN, sheet.y,
                    _("{n} heures d'horloge avec du trafic, {rate} QSO/h en moyenne sur ces heures-là.",
                      n=timeline["active_hours"], rate=f"{timeline['per_hour']:g}"),
                    size=8.5, color=MUTED, width=sheet.width)
    sheet.y += m["gap"] + 8

    # ── Bandes et modes ───────────────────────────────────────────────────
    lines = max(len(stats["by_band"][:8]), len(stats["by_mode"][:8]))
    sheet = book.room(34 + 17 * lines) if not single else book.sheet
    sheet.section(_("Bandes et modes"),
                  _("{located} / {total} stations localisées",
                    located=data["map_data"]["located"], total=data["map_data"]["stations"]))
    half = (sheet.width - 26) / 2
    start_y = sheet.y
    bands = [(b["band"] or "?", b["n"], BAND_COLOR) for b in stats["by_band"][:8]]
    end_left = sheet.bars(MARGIN, half, bands, stats["total"])
    modes = [(mo["mode"] or "?", mo["n"],
              pdf.hex_color(data["style"]["mode_colors"].get(mo["mode"],
                                                             data["style"]["mode_default"])))
             for mo in stats["by_mode"][:8]]
    sheet.y = start_y
    end_right = sheet.bars(MARGIN + half + 26, half, modes, stats["total"])
    sheet.y = max(end_left, end_right) + m["gap"]

    # ── Satellites (à la suite des modes) ─────────────────────────────────
    sats = data["sats"] if opts["sats"] else {"by_sat": [], "total": 0, "share": 0}
    if sats["by_sat"]:
        shown = sats["by_sat"][:8]
        sheet = book.room(34 + 17 * len(shown)) if not single else book.sheet
        sheet.section(_("Satellites"),
                      _("{n} QSO par satellite · {share} % du log",
                        n=sats["total"], share=f"{sats['share']:g}"), icon="sat")
        sheet.y = sheet.bars(MARGIN, sheet.width * 0.58,
                             [(item["sat"], item["n"], pdf.hex_color("#7048a8"))
                              for item in shown],
                             max(sats["total"], 1)) + m["gap"]

    # ── Entités DXCC ──────────────────────────────────────────────────────
    entities = dxcc["entities"]
    if entities:
        if opts["dxcc_all"]:
            columns = 3
            sheet = book.room(34 + m["grid"] * 6) if not single else book.sheet
            sheet.section(_("Entités DXCC contactées"), _("{n} entités", n=dxcc["count"]))
            start = 0
            while start < len(entities):
                room = sheet.room_left() - 10
                per_col = max(int(room // m["grid"]), 3)
                chunk = entities[start:start + per_col * columns]
                if single and start + len(chunk) < len(entities):
                    # Une seule page : on garde ce qui tient et on annonce le reste.
                    keep = max(per_col * columns - columns, columns)
                    chunk = entities[start:start + keep]
                    sheet.flag_grid(chunk, columns=columns,
                                    more=len(entities) - start - len(chunk))
                    start = len(entities)
                    break
                sheet.flag_grid(chunk, columns=columns)
                start += len(chunk)
                if start < len(entities):
                    sheet = book.new_sheet()
        else:
            sheet = book.room(34 + m["row"] * 10) if not single else book.sheet
            sheet.section(_("Entités DXCC contactées"), _("Les dix premières"))
            sheet.y = sheet.table(MARGIN, sheet.width * 0.58,
                                  (_("Entité"), _("Stations"), _("QSO")),
                                  [(e["dxcc_name"], str(e["stations"]), str(e["qsos"]))
                                   for e in entities[:10]],
                                  (sheet.width * 0.58 - 96, 56, 40)) + m["gap"]

    # ── Chasseurs (facultatif) ────────────────────────────────────────────
    top = hunters[:opts["hunters"]] if opts["hunters"] else []
    if top:
        columns = 2 if len(top) > 8 else 1
        rows = (len(top) + columns - 1) // columns
        if single:
            sheet = book.sheet
            fits = max(int((sheet.room_left() - 26) // m["row"]), 0)
            if fits < rows:
                rows = fits
                top = top[:rows * columns]
        else:
            sheet = book.room(30 + m["row"] * min(rows, 16))
        if top:
            sheet.section(_("Meilleurs chasseurs"), _("Les {n} premiers", n=len(top)))
            col_w = (sheet.width - 26) / columns if columns > 1 else sheet.width * 0.58
            start_y, lowest = sheet.y, sheet.y
            for col in range(columns):
                part = top[col * rows:(col + 1) * rows]
                if not part:
                    continue
                sheet.y = start_y
                lowest = max(lowest, sheet.table(
                    MARGIN + col * (col_w + 26), col_w,
                    (_("Indicatif"), _("Pays"), _("QSO")),
                    [(h["call"], h["dxcc_name"], str(h["qsos"])) for h in part],
                    (68, col_w - 108, 40),
                ))
            sheet.y = lowest

    stamp = _("{call} · {club}", call=call, club=club_line) if club_line else call
    for number, page in enumerate(doc.pages, start=1):
        _Sheet.footer_on(page, stamp, number, len(doc.pages))
    return doc


def build_report(station: str | None = None) -> bytes:
    """PDF du bilan d'une activation (défaut : l'indicatif en cours).

    Le contenu suit les options des Réglages. Le document est composé au large ;
    s'il ne déborde que d'un cheveu — ou si « une seule page » est demandé — il
    est recomposé plus serré (barres du rythme, interlignes, puis bandeau de
    titre) avant, en dernier recours, de couper les listes.
    """
    st = activation.get_station(station) if station else activation.current_station()
    if st is None:
        raise ValueError(_("indicatif inconnu"))
    call = st["callsign"]
    opts = activation.get_report_options()
    data = _gather(call)
    club = club_config()
    name, sign = (club.get("name") or "").strip(), (club.get("callsign") or "").strip()
    # « Radioclub F6ABC · F6ABC » : on ne répète pas l'indicatif déjà dans le nom.
    club_line = name if (not sign or sign.upper() in name.upper()) else \
        " · ".join(bit for bit in (name, sign) if bit)
    logo = None
    info = activation.logo_info()
    if info:
        try:
            logo = info["path"].read_bytes()
        except OSError:
            logo = None

    def compose(level: int, single: bool = False) -> pdf.Pdf:
        return _compose(call, data, opts, DENSITIES[level], logo, club_line, single)

    doc = compose(0)
    if len(doc.pages) == 1:
        return doc.output()
    if opts["one_page"]:
        for level in range(len(DENSITIES)):
            tried = compose(level)
            if len(tried.pages) == 1:
                return tried.output()
        return compose(len(DENSITIES) - 1, single=True).output()
    # Débordement minime (la deuxième page est presque vide) : on resserre d'un
    # cran plutôt que d'imprimer une page pour trois lignes.
    if len(doc.pages) == 2 and _tail_height(doc) < 230:
        for level in (1, 2):
            tried = compose(level)
            if len(tried.pages) == 1:
                return tried.output()
    return doc.output()


def _tail_height(doc: pdf.Pdf) -> float:
    """Hauteur occupée sur la dernière page (repère du « ça déborde de peu »)."""
    page = doc.pages[-1]
    stream = page.stream()
    lowest = 0.0
    for chunk in stream.split(b"\n"):
        for token in chunk.replace(b"(", b" ").split():
            try:
                value = float(token)
            except ValueError:
                continue
            if 0 < value < page.height:
                lowest = max(lowest, page.height - value)
    return lowest
