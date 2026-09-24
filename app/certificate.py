"""Certificat PDF d'un chasseur, à partir du log de l'activation.

Le dessin n'est pas refait ici : il vient du module :mod:`app.tmcert`, fourni
comme référence visuelle (voir ``app/tmcert/SPEC.md``). Ce fichier ne fait que
préparer les données — QSO du chasseur, classement, points, logo du club — et
appeler ``tmcert.render``.

Reportlab est nécessaire (il porte les polices du certificat). Sans lui, la
fonction le dit clairement plutôt que de planter au milieu d'une page.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app import activation
from app.config import club_config
from app.i18n import gettext as _

MAX_QSO = 200          # au-delà, l'annexe deviendrait un annuaire


class CertificateUnavailable(RuntimeError):
    """Le moteur de certificats n'est pas installé (reportlab manquant)."""


def engine_ready() -> bool:
    """Le moteur de certificats est-il utilisable sur cette machine ?"""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


def _band_label(band: str) -> str:
    """« 40M » → « 40 m », « 70CM » → « 70 cm » (présentation du certificat)."""
    raw = (band or "").strip().upper()
    if raw.endswith("CM"):
        return f"{raw[:-2]} cm"
    if raw.endswith("MM"):
        return f"{raw[:-2]} mm"
    if raw.endswith("M"):
        return f"{raw[:-1]} m"
    return raw


def _period(station: dict[str, Any]) -> str:
    """« du 6 au 20 septembre 2026 », d'après les dates de la fiche."""
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


def _day(when: date, year: bool = False) -> str:
    months = [_("janvier"), _("février"), _("mars"), _("avril"), _("mai"), _("juin"),
              _("juillet"), _("août"), _("septembre"), _("octobre"), _("novembre"),
              _("décembre")]
    out = f"{when.day} {months[when.month - 1]}"
    return f"{out} {when.year}" if year else out


def hunter_data(call: str, station: str | None = None) -> dict[str, Any] | None:
    """Données du certificat d'un chasseur, ou None s'il n'est pas au log."""
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
    bareme = {**rule["mode_points"], "*": rule["mode_default"]} if rule["enabled"] \
        else {"*": 1}
    # Les points imprimés sont CEUX DE L'APPLICATION : même règle que le
    # classement public, doublons bande×mode compris.
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
            "heure_utc": f"{hhmm[:2]}:{hhmm[2:]}",
            "bande": _band_label(c["band"]),
            "mode": (c["mode"] or "").upper(),
            "rst_envoye": c["rst_sent"] or "",
            "rst_recu": c["rst_rcvd"] or "",
        }
        if c["freq_mhz"]:
            entry["freq_mhz"] = float(c["freq_mhz"])
        entry["points"] = (activation.qso_points(entry["mode"], km, rule)
                           if rule["enabled"] else 1)
        qso.append(entry)
    _apply_best_per_pair(qso, rule)

    data: dict[str, Any] = {
        "activation": {
            "indicatif": target,
            "evenement": st.get("label") or target,
            "periode": _period(st),
            # L'indicatif spécial prend la place du gros titre, souligné de son
            # propre morse ; le libellé de l'activation vient juste dessous.
            "titre": target,
            "sous_titre": (st.get("label") or "").strip().upper(),
            "morse": target,
        },
        "options": {"max_qso": options["max_qso"], "annexe": options["annexe"]},
        "destinataire": {"indicatif": cs, "nom": name, "locator": grid},
        "classement": ({"position": place["rank"], "total": len(ranking)}
                       if place and options["ranking"] else None),
        "bareme": bareme,
        "qso": qso,
        "certificat": {
            "numero": f"{target}-{cs}".replace("/", "-"),
            "date_emission": datetime.now(UTC).date().isoformat(),
            "gestionnaire": manager,
        },
    }
    if options["mention"]:
        data["mention"] = options["mention"]
    logo = _certificate_logo()
    if logo:
        data["logo_path"] = str(logo)
    flag = _flag_path(options["flag"], target)
    if flag:
        data["drapeau"] = str(flag)
    if options["border"]:
        data["liseret"] = options["border_colors"]
    if options["emblem"]:
        data["embleme"] = {"texte": options["emblem_text"] or "HAM RADIO"}
    if options["ham_symbol"]:
        data["symbole_ra"] = True
    if options["qr_url"]:
        data["qr_url"] = options["qr_url"]
    return data


FLAGS_DIR = Path(__file__).resolve().parent.parent / "static" / "vendor" / "flags"


def _flag_path(choice: str, station: str) -> Path | None:
    """Vignette du drapeau choisi (« auto » = d'après l'indicatif spécial)."""
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
    """Un seul QSO compté par bande × mode, comme au classement.

    Le meilleur de chaque couple garde ses points, les autres tombent à zéro :
    le total du certificat colle alors au score affiché sur la page publique.
    Sans règle de points, c'est le nombre de couples bande×mode qui fait foi —
    c'est déjà le critère du classement.
    """
    if rule["enabled"] and not rule["unique_band_mode"]:
        return
    best: dict[tuple[str, str], int] = {}
    for entry in qso:
        key = (entry["bande"], entry["mode"])
        best[key] = max(best.get(key, 0), entry["points"])
    kept: set[tuple[str, str]] = set()
    for entry in qso:
        key = (entry["bande"], entry["mode"])
        if key not in kept and entry["points"] == best[key]:
            kept.add(key)
        else:
            entry["points"] = 0


LOGO_SIDE = 420        # le logo est imprimé sur ~6 cm : 420 px suffisent


def _certificate_logo() -> Path | None:
    """Logo du club, réduit et mis en cache pour le certificat.

    Le logo déposé par l'admin peut peser près d'un mégaoctet : embarqué tel
    quel dans chaque certificat, il ferait des PDF de 1 Mo pour une vignette de
    six centimètres. On garde la transparence (le disque du certificat est doré)
    et on ne refait la réduction que si le fichier d'origine a changé.
    """
    info = activation.logo_info()
    if info is None:
        return None
    source = info["path"]
    cached = source.parent / "logo-certificat.png"
    if cached.is_file() and cached.stat().st_mtime >= source.stat().st_mtime:
        return cached
    try:
        from PIL import Image      # livré avec reportlab

        with Image.open(source) as img:
            small = img.convert("RGBA")
            small.thumbnail((LOGO_SIDE, LOGO_SIDE), Image.LANCZOS)
            small.save(cached, "PNG", optimize=True)
    except Exception:              # noqa: BLE001 — un logo illisible ne doit pas tout bloquer
        return source
    return cached


def build_certificate(call: str, station: str | None = None) -> bytes | None:
    """PDF du certificat d'un chasseur (None s'il n'a aucun QSO avec nous)."""
    if not engine_ready():
        raise CertificateUnavailable(
            _("moteur de certificats absent : installez reportlab (pip install reportlab)")
        )
    data = hunter_data(call, station)
    if data is None:
        return None
    from app.tmcert import render        # importé tard : reportlab est lourd

    return render(data)
