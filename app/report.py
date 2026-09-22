"""Rapport PDF d'une activation : bilan illustré, pour l'administrateur.

Une page A4 (deux si le palmarès est long) reprenant ce qui compte : le titre
de l'activation, les compteurs, le rythme (par jour et par heure), la
répartition par bande et par mode, les entités DXCC et les meilleurs chasseurs.

Dessiné avec :mod:`app.pdf` — aucune dépendance extérieure, donc un Pi hors
ligne sort le même document qu'un serveur.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app import activation, pdf
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
TOP_BAND = 104.0


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


class _Sheet:
    """Une page du rapport, avec un curseur vertical et les blocs de dessin."""

    def __init__(self, doc: pdf.Pdf) -> None:
        self.page = doc.page()
        self.y = MARGIN
        self.width = self.page.width - 2 * MARGIN

    # ── briques ───────────────────────────────────────────────────────────
    def title_band(self, callsign: str, label: str, period: str, club: str) -> None:
        page = self.page
        page.rect(0, 0, page.width, TOP_BAND, fill=ACCENT)
        page.rect(0, TOP_BAND - 6, page.width, 6, fill=ACCENT_DARK)
        page.text(MARGIN, 22, callsign, size=30, bold=True, color=WHITE)
        if label:
            page.text(MARGIN, 58, label, size=12.5, color=WHITE, width=self.width * 0.62)
        line = " · ".join(bit for bit in (period, club) if bit)
        page.text(MARGIN, 78, line, size=9.5, color=pdf.mix(WHITE, ACCENT, 0.35))
        stamp = datetime.now(UTC).strftime("%d/%m/%Y %H:%M")
        page.text(MARGIN, 26, _("Rapport d'activité"), size=11, bold=True, color=WHITE,
                  align="right", width=self.width)
        page.text(MARGIN, 44, _("établi le {when} UTC", when=stamp), size=9,
                  color=pdf.mix(WHITE, ACCENT, 0.35), align="right", width=self.width)
        self.y = TOP_BAND + 24

    def section(self, title: str, hint: str = "") -> None:
        self.page.text(MARGIN, self.y, title.upper(), size=10.5, bold=True, color=ACCENT)
        if hint:
            self.page.text(MARGIN, self.y + 1, hint, size=8.5, color=MUTED,
                           align="right", width=self.width)
        self.y += 15
        self.page.line(MARGIN, self.y, MARGIN + self.width, self.y, color=LINE, width=0.8)
        self.y += 12

    def kpis(self, items: list[tuple[str, str]]) -> None:
        """Bandeau de compteurs : un pavé coloré par chiffre clé."""
        gap = 9.0
        count = max(len(items), 1)
        w = (self.width - gap * (count - 1)) / count
        for i, (value, label) in enumerate(items):
            color = pdf.hex_color(KPI_COLORS[i % len(KPI_COLORS)])
            x = MARGIN + i * (w + gap)
            self.page.rect(x, self.y, w, 54, fill=pdf.mix(WHITE, color, 0.10),
                           stroke=pdf.mix(WHITE, color, 0.35), radius=5)
            self.page.rect(x, self.y, 3.5, 54, fill=color)
            self.page.text(x + 10, self.y + 9, value, size=21, bold=True, color=color,
                           width=w - 16)
            self.page.text(x + 10, self.y + 36, label.upper(), size=7.5, color=MUTED,
                           width=w - 16)
        self.y += 54 + 18

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
                self.page.rect(x - 3, y - 2, width + 6, 14, fill=PANEL)
            cx = x
            for cell, w in zip(row, widths):
                align = "left" if w == widths[0] else "right"
                self.page.text(cx, y, cell, size=8.5, color=INK, align=align, width=w)
                cx += w
            y += 14
        return y

    def footer(self, text: str, page_no: int, pages: int) -> None:
        y = self.page.height - 26
        self.page.line(MARGIN, y - 8, MARGIN + self.width, y - 8, color=LINE, width=0.7)
        self.page.text(MARGIN, y, text, size=7.5, color=MUTED, width=self.width * 0.7)
        self.page.text(MARGIN, y, _("Page {n}/{total}", n=page_no, total=pages), size=7.5,
                       color=MUTED, align="right", width=self.width)


def build_report(station: str | None = None) -> bytes:
    """PDF du bilan d'une activation (défaut : l'indicatif en cours)."""
    st = activation.get_station(station) if station else activation.current_station()
    if st is None:
        raise ValueError(_("indicatif inconnu"))
    call = st["callsign"]
    stats = activation.stats(call)
    timeline = activation.qso_timeline(call)
    dxcc = activation.dxcc_table(call)
    hunters = activation.hunters_ranking(None, call)
    map_data = activation.map_data(call)
    style = activation.get_map_style(call)
    club = club_config()
    name, sign = (club.get("name") or "").strip(), (club.get("callsign") or "").strip()
    # « Radioclub F6ABC · F6ABC » : on ne répète pas l'indicatif déjà dans le nom.
    club_line = name if (not sign or sign.upper() in name.upper()) else \
        " · ".join(bit for bit in (name, sign) if bit)

    doc = pdf.Pdf()
    doc.title = _("{call} — rapport d'activité", call=call)
    doc.author = club_line or call

    sheet = _Sheet(doc)
    sheet.title_band(call, st.get("label") or "", _period(st, timeline), club_line)

    # ── Compteurs ─────────────────────────────────────────────────────────
    sheet.kpis([
        (str(stats["total"]), _("QSO")),
        (str(dxcc["count"]), _("entités DXCC")),
        (str(len({h["call"] for h in hunters})), _("stations")),
        (str(len(stats["by_op"])), _("opérateurs")),
        (f"{timeline['per_hour']:g}", _("QSO/h en trafic")),
    ])

    # ── Rythme ────────────────────────────────────────────────────────────
    best_day = timeline["best_day"]
    hint = ""
    if best_day:
        hint = _("Meilleure journée : {day} ({n} QSO)",
                 day=_date_fr(best_day["day"]), n=best_day["n"])
    sheet.section(_("Rythme, jour par jour"), hint)
    days = timeline["by_day"]
    if days:
        sheet.columns([d["n"] for d in days], [_date_fr(d["day"]) for d in days],
                      height=92, color=ACCENT, every=max(1, len(days) // 14))
    else:
        sheet.page.text(MARGIN, sheet.y, _("Aucun QSO enregistré."), size=9, color=MUTED)
        sheet.y += 20

    slot = timeline["best_slot"]
    hint = ""
    if slot["n"]:
        hint = _("Heure la plus forte : {day} à {hour} h UTC ({n} QSO)",
                 day=_date_fr(slot["day"]), hour=slot["hour"], n=slot["n"])
    sheet.y += 6
    sheet.section(_("Rythme, heure par heure (UTC)"), hint)
    sheet.columns(timeline["by_hour"], [f"{h:02d}" for h in range(24)],
                  height=74, color=pdf.hex_color("#0f9d8f"), every=2, value_labels=False)
    active = _("{n} heures d'horloge avec du trafic, {rate} QSO/h en moyenne sur ces heures-là.",
               n=timeline["active_hours"], rate=f"{timeline['per_hour']:g}")
    sheet.page.text(MARGIN, sheet.y, active, size=8.5, color=MUTED, width=sheet.width)
    sheet.y += 22

    # ── Bandes et modes ───────────────────────────────────────────────────
    sheet.section(_("Bandes et modes"),
                  _("{located} / {total} stations localisées",
                    located=map_data["located"], total=map_data["stations"]))
    half = (sheet.width - 26) / 2
    start_y = sheet.y
    bands = [(b["band"] or "?", b["n"], BAND_COLOR) for b in stats["by_band"][:7]]
    end_left = sheet.bars(MARGIN, half, bands, stats["total"])
    modes = [(m["mode"] or "?", m["n"],
              pdf.hex_color(style["mode_colors"].get(m["mode"], style["mode_default"])))
             for m in stats["by_mode"][:7]]
    sheet.y = start_y
    end_right = sheet.bars(MARGIN + half + 26, half, modes, stats["total"])
    sheet.y = max(end_left, end_right) + 12

    # ── Palmarès (sur la première page si la place le permet) ─────────────
    entities = dxcc["entities"][:10]
    top_hunters = hunters[:10]
    needed = 26 + 14 * max(len(entities), len(top_hunters))
    if sheet.y + needed > sheet.page.height - 48:      # place du pied de page
        sheet.footer(doc.title, 1, 2)
        sheet = _Sheet(doc)
        sheet.y = MARGIN + 4
    sheet.section(_("Entités et chasseurs"), _("Les dix premiers"))
    start_y = sheet.y
    end_left = sheet.table(
        MARGIN, half, (_("Entité"), _("Stations"), _("QSO")),
        [(e["dxcc_name"], str(e["stations"]), str(e["qsos"])) for e in entities],
        (half - 96, 56, 40),
    )
    sheet.y = start_y
    end_right = sheet.table(
        MARGIN + half + 26, half, (_("Indicatif"), _("Pays"), _("QSO")),
        [(h["call"], h["dxcc_name"], str(h["qsos"])) for h in top_hunters],
        (68, half - 108, 40),
    )
    sheet.y = max(end_left, end_right)

    pages = len(doc.pages)
    sheet.footer(_("{call} · {club}", call=call, club=club_line) if club_line else call,
                 pages, pages)
    return doc.output()
