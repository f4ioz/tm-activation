"""A hunter's PDF certificate, built from the activation log.

The drawing is not done here: it comes from :mod:`app.tmcert` (layout and data
format in ``app/tmcert/SPEC.md``). This file only prepares the data — the
hunter's QSOs, ranking, points, club logo — and calls ``tmcert.render``.

Reportlab is required (it carries the certificate fonts). Without it, the
function says so plainly instead of failing halfway through a page.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app import activation, i18n
from app.config import club_config
from app.i18n import gettext as _

MAX_QSO = 200          # beyond that, the appendix would turn into a phone book


class CertificateUnavailable(RuntimeError):
    """The certificate engine is not installed (reportlab missing)."""


def engine_ready() -> bool:
    """Can the certificate engine be used on this machine?"""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


def _band_label(band: str) -> str:
    """"40M" → "40 m", "70CM" → "70 cm" (certificate display)."""
    raw = (band or "").strip().upper()
    if raw.endswith("CM"):
        return f"{raw[:-2]} cm"
    if raw.endswith("MM"):
        return f"{raw[:-2]} mm"
    if raw.endswith("M"):
        return f"{raw[:-1]} m"
    return raw


def _period(station: dict[str, Any]) -> str:
    """"du 6 au 20 septembre 2026", from the station record's dates."""
    start, end = (station.get("start_date") or ""), (station.get("end_date") or "")
    if not start:
        return ""
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) if end else None
    except ValueError:
        return ""
    if last and last != first:
        return _("du {start} au {end}", start=_day(first), end=_day(last, year=True))
    return _("le {day}", day=_day(first, year=True))


def _months() -> list[str]:
    return [_("janvier"), _("février"), _("mars"), _("avril"), _("mai"), _("juin"),
            _("juillet"), _("août"), _("septembre"), _("octobre"), _("novembre"),
            _("décembre")]


def _labels() -> dict[str, Any]:
    """Printed texts of the certificate, in the language of the request.

    French is also tmcert's default wording; other languages get their date
    and number formats too ("11 Sep 2026" in the tables, "11 September 2026"
    in the footer, "14.190")."""
    labels: dict[str, Any] = {
        "title": _("CERTIFICAT"),
        "subtitle": _("ACTIVATION SPÉCIALE · {event}"),
        "awarded_to": _("CE CERTIFICAT EST DÉCERNÉ À"),
        "award_text": _("pour avoir contacté la station spéciale {callsign}, activée lors du {event} {period}."),
        "columns": [_("DATE"), "UTC", _("BANDE"), _("FRÉQ. MHz"), _("MODE"), _("RST ENV."), _("RST REÇU")],
        "more_qso": _("… et {n} autres QSO"),
        "see_appendix": " — " + _("journal complet en annexe"),
        # Leading spaces separate the summary items: added here, the catalog
        # keys have none.
        "summary": [_("QSO") + " "] + ["   " + _(k) + " " for k in ("BANDES", "MODES", "POINTS")],
        "manager": _("GESTIONNAIRE"),
        "number": _("N° DU CERTIFICAT"),
        "date": _("DATE"),
        "rank_of": _("SUR {total}"),
        "appendix_title": _("JOURNAL DES CONTACTS · {callsign}"),
        "appendix_header": _("{callsign} · {event} · annexe {n}/{total}"),
        "appendix_footer": _("{count} QSO · {bands} bandes · {modes} · {points} points · certificat n° {number}"),
        "pdf_title": _("Certificat {callsign} — {recipient}"),
    }
    if i18n.current() != i18n.DEFAULT:
        long_names = _months()
        labels.update(date_format="{d:02d} {mon} {y}", issue_date_format="{d} {month} {y}",
                      months=[m[:3] for m in long_names],   # Jan, Feb… (English)
                      months_long=long_names, decimal=".")
    return labels


def _day(when: date, year: bool = False) -> str:
    months = _months()
    out = f"{when.day} {months[when.month - 1]}"
    return f"{out} {when.year}" if year else out


def hunter_data(call: str, station: str | None = None) -> dict[str, Any] | None:
    """A hunter's certificate data, or None if they are not in the log."""
    cs = (call or "").strip().upper()
    st = activation.get_station(station) if station else activation.current_station()
    if st is None or not activation.valid_callsign(cs):
        return None
    target = st["callsign"]
    contacts = [c for c in activation.list_contacts(station=target) if c["call"] == cs]
    if not contacts:
        return None
    contacts.sort(key=lambda c: (c["qso_date"], c["time_on"]))

    ranking = activation.hunters_ranking(None, target)
    place = next((h for h in ranking if h["call"] == cs), None)
    rule = activation.get_scoring(target)
    scoring = {**rule["mode_points"], "*": rule["mode_default"]} if rule["enabled"] \
        else {"*": 1}
    # The printed points are THE APPLICATION'S: same rule as the public
    # ranking, band×mode duplicates included.
    km = activation.distance_km(activation.my_gridsquare(target),
                                activation.call_grids(target).get(cs, ""))
    club = club_config()
    manager = (club.get("name") or "").strip() or target
    options = activation.get_certificate_options()
    grid = activation.call_grids(target).get(cs, "")
    name = ""
    if options["names"]:
        row = activation.callbook_get(cs)
        if row and row["status"] == "ok":
            name = " ".join(bit for bit in ((row["fname"] or "").strip(),
                                            (row["name"] or "").strip().upper()) if bit)

    qso = []
    for c in contacts[:MAX_QSO]:
        day = c["qso_date"]
        hhmm = (c["time_on"] or "0000")[:4].ljust(4, "0")
        entry: dict[str, Any] = {
            "date": f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day,
            "time_utc": f"{hhmm[:2]}:{hhmm[2:]}",
            "band": _band_label(c["band"]),
            "mode": (c["mode"] or "").upper(),
            "rst_sent": c["rst_sent"] or "",
            "rst_rcvd": c["rst_rcvd"] or "",
        }
        if c["freq_mhz"]:
            entry["freq_mhz"] = float(c["freq_mhz"])
        entry["points"] = (activation.qso_points(entry["mode"], km, rule)
                           if rule["enabled"] else 1)
        qso.append(entry)
    _apply_best_per_pair(qso, rule)

    data: dict[str, Any] = {
        "activation": {
            "callsign": target,
            "event": st.get("label") or target,
            "period": _period(st),
            # The special callsign takes the big title's place, underlined with
            # its own Morse code; the activation label comes right below.
            "title": target,
            "subtitle": (st.get("label") or "").strip().upper(),
            "morse": target,
        },
        "options": {"max_qso": options["max_qso"], "appendix": options["appendix"]},
        "recipient": {"callsign": cs, "name": name, "locator": grid},
        "ranking": ({"position": place["rank"], "total": len(ranking),
                     "suffix": i18n.ordinal(place["rank"])}
                    if place and options["ranking"] else None),
        "scoring": scoring,
        "qso": qso,
        "certificate": {
            "number": f"{target}-{cs}".replace("/", "-"),
            "issue_date": datetime.now(UTC).date().isoformat(),
            "manager": manager,
        },
    }
    if options["mention"]:
        data["footnote"] = options["mention"]
    logo = _certificate_logo()
    if logo:
        data["logo_path"] = str(logo)
    flag = _flag_path(options["flag"], target)
    if flag:
        data["flag"] = str(flag)
    if options["border"]:
        data["border"] = options["border_colors"]
    if options["emblem"]:
        data["emblem"] = {"text": options["emblem_text"] or "HAM RADIO"}
    if options["ham_symbol"]:
        data["ham_symbol"] = True
    data["labels"] = _labels()
    if options["qr_url"]:
        data["qr_url"] = options["qr_url"]
    return data


FLAGS_DIR = Path(__file__).resolve().parent.parent / "static" / "vendor" / "flags"


def _flag_path(choice: str, station: str) -> Path | None:
    """Thumbnail of the chosen flag ("auto" = from the special callsign)."""
    from app import dxcc_flags

    code = (choice or "").strip()
    if not code:
        return None
    if code.lower() == "auto":
        code = dxcc_flags.entity_for_call(station)[0]
    if not code:
        return None
    path = FLAGS_DIR / f"{dxcc_flags.flag_file(code)}.png"
    return path if path.is_file() else None


def _apply_best_per_pair(qso: list[dict[str, Any]], rule: dict[str, Any]) -> None:
    """Only one QSO counted per band × mode, as in the ranking.

    The best of each pair keeps its points, the others drop to zero: the
    certificate total then matches the score shown on the public page.
    Without a points rule, the number of band×mode pairs is what counts —
    that is already the ranking criterion.
    """
    if rule["enabled"] and not rule["unique_band_mode"]:
        return
    best: dict[tuple[str, str], int] = {}
    for entry in qso:
        key = (entry["band"], entry["mode"])
        best[key] = max(best.get(key, 0), entry["points"])
    kept: set[tuple[str, str]] = set()
    for entry in qso:
        key = (entry["band"], entry["mode"])
        if key not in kept and entry["points"] == best[key]:
            kept.add(key)
        else:
            entry["points"] = 0


LOGO_SIDE = 420        # the logo is printed about 6 cm wide: 420 px is plenty


def _certificate_logo() -> Path | None:
    """Club logo, shrunk and cached for the certificate.

    The logo uploaded by the admin can weigh nearly a megabyte: embedded as is
    in every certificate, it would make 1 MB PDFs for a six-centimetre
    thumbnail. Transparency is kept (the certificate disc is gold) and the
    shrinking is only redone when the original file has changed.
    """
    info = activation.logo_info()
    if info is None:
        return None
    source = info["path"]
    cached = source.parent / "logo-certificat.png"
    if cached.is_file() and cached.stat().st_mtime >= source.stat().st_mtime:
        return cached
    try:
        from PIL import Image      # shipped with reportlab

        with Image.open(source) as img:
            small = img.convert("RGBA")
            small.thumbnail((LOGO_SIDE, LOGO_SIDE), Image.LANCZOS)
            small.save(cached, "PNG", optimize=True)
    except Exception:              # noqa: BLE001 — an unreadable logo must not block everything
        return source
    return cached


def build_certificate(call: str, station: str | None = None) -> bytes | None:
    """A hunter's certificate PDF (None if they have no QSO with us)."""
    if not engine_ready():
        raise CertificateUnavailable(
            _("moteur de certificats absent : installez reportlab (pip install reportlab)")
        )
    data = hunter_data(call, station)
    if data is None:
        return None
    from app.tmcert import render        # imported late: reportlab is heavy

    return render(data)
