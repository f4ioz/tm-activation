"""Spots DX de l'indicatif activé (« suis-je spotté ? »).

Interroge un réseau de spots public — DXWatch, puis HamQTH en secours — et
garde le résultat en mémoire quelques dizaines de secondes : la page de log
demande toutes les minutes, sans charger les serveurs.

Sans Internet (réseau du club isolé), rien ne casse : la liste est vide et le
panneau disparaît. Aucune clé ni inscription n'est nécessaire.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CACHE_TTL = 60.0          # une interrogation par minute et par indicatif
MAX_AGE_MIN = 360         # « suis-je spotté ? » : au-delà de 6 h, ce n'est plus l'actualité
REQUEST_TIMEOUT = 6.0
USER_AGENT = "tm-activation (ham radio special callsign logger)"
DXWATCH_URL = "https://dxwatch.com/dxsd1/s.php"
HAMQTH_URL = "https://www.hamqth.com/dxc_csv.php"

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_lock = threading.Lock()
_RE_WHEN = re.compile(r"^(\d{2})(\d{2})z\s+(\d{1,2})\s+(\w{3})", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def band_of(freq_khz: float) -> str:
    """Bande amateur d'une fréquence en kHz (« 20M »), '' si hors bande connue."""
    plan = [(1800, 2000, "160M"), (3500, 4000, "80M"), (5250, 5450, "60M"), (7000, 7300, "40M"),
            (10100, 10150, "30M"), (14000, 14350, "20M"), (18068, 18168, "17M"),
            (21000, 21450, "15M"), (24890, 24990, "12M"), (28000, 29700, "10M"),
            (50000, 54000, "6M"), (70000, 71000, "4M"), (144000, 148000, "2M"),
            (430000, 440000, "70CM"), (1240000, 1300000, "23CM")]
    for low, high, band in plan:
        if low <= freq_khz <= high:
            return band
    return ""


def _age_minutes(when: str) -> int | None:
    """« 1433z 20 Sep » → âge en minutes (None si illisible)."""
    match = _RE_WHEN.match((when or "").strip())
    if not match:
        return None
    hour, minute, day, month = match.groups()
    try:
        index = _MONTHS.index(month[:3].lower()) + 1
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    try:
        stamp = datetime(now.year, index, int(day), int(hour), int(minute), tzinfo=timezone.utc)
    except ValueError:
        return None
    if stamp - now > _ONE_DAY:          # spot de l'an dernier (passage de janvier)
        stamp = stamp.replace(year=now.year - 1)
    return max(0, int((now - stamp).total_seconds() // 60))


_ONE_DAY = (datetime(2000, 1, 2, tzinfo=timezone.utc) - datetime(2000, 1, 1, tzinfo=timezone.utc))


def _from_dxwatch(call: str, limit: int) -> list[dict[str, Any]]:
    """Spots dont ``call`` est la station entendue (paramètre cde)."""
    response = httpx.get(DXWATCH_URL, params={"s": 0, "r": limit, "cde": call},
                         timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    spots = []
    for row in (response.json().get("s") or {}).values():
        if len(row) < 5 or (row[0] or "").upper() != call:
            continue
        try:
            freq = float(row[1])
        except (TypeError, ValueError):
            continue
        spots.append({"dx": call, "freq_khz": freq, "spotter": (row[2] or "").upper(),
                      "comment": (row[3] or "").replace("&gt;", ">").replace("&lt;", "<")[:60],
                      "when": row[4], "age_min": _age_minutes(row[4]), "band": band_of(freq)})
    return spots


def _from_hamqth(call: str, limit: int) -> list[dict[str, Any]]:
    """Secours : les derniers spots mondiaux, filtrés sur notre indicatif."""
    response = httpx.get(HAMQTH_URL, params={"limit": 200}, timeout=REQUEST_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    spots = []
    for line in response.text.splitlines():
        parts = line.split("^")
        if len(parts) < 5 or parts[0].strip().upper() != call:
            continue
        try:
            freq = float(parts[1])
        except ValueError:
            continue
        spots.append({"dx": call, "freq_khz": freq, "spotter": parts[2].strip().upper(),
                      "comment": (parts[3] or "").strip()[:60], "when": parts[4].strip(),
                      "age_min": None, "band": band_of(freq)})
    return spots[:limit]


def recent_spots(call: str, limit: int = 5, force: bool = False) -> list[dict[str, Any]]:
    """Derniers spots de cet indicatif, du plus récent au plus ancien ([] si
    Internet manque ou si personne ne nous a spotté)."""
    cs = (call or "").strip().upper()
    if not cs:
        return []
    now = time.time()
    with _lock:
        cached = _cache.get(cs)
        if cached and not force and now - cached[0] < CACHE_TTL:
            return cached[1][:limit]
    spots: list[dict[str, Any]] = []
    for source in (_from_dxwatch, _from_hamqth):
        try:
            spots = source(cs, max(limit, 5))
        except Exception:  # noqa: BLE001 — pas de réseau, source en panne : on passe
            logger.debug("spots : source %s indisponible", source.__name__, exc_info=True)
            continue
        if spots:
            break
    spots = [s for s in spots if s["age_min"] is None or s["age_min"] <= MAX_AGE_MIN]
    spots.sort(key=lambda s: (s["age_min"] is None, s["age_min"] or 0))
    with _lock:
        _cache[cs] = (now, spots)
    return spots[:limit]


def clear_cache() -> None:
    with _lock:
        _cache.clear()
