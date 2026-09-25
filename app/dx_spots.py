"""DX spots of the activated callsign ("am I spotted?").

Queries a public spot network — DXWatch, then HamQTH as a fallback — and keeps
the result in memory for a few tens of seconds: the log page asks every minute
without loading the servers.

Without Internet (isolated club network), nothing breaks: the list is empty and
the panel disappears. No key or registration is required.
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

CACHE_TTL = 60.0          # one query per minute and per callsign
MAX_AGE_MIN = 360         # "am I spotted?": beyond 6 h, it is no longer current news
REQUEST_TIMEOUT = 6.0
USER_AGENT = "tm-activation (ham radio special callsign logger)"
DXWATCH_URL = "https://dxwatch.com/dxsd1/s.php"
HAMQTH_URL = "https://www.hamqth.com/dxc_csv.php"

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_lock = threading.Lock()
_RE_WHEN = re.compile(r"^(\d{2})(\d{2})z\s+(\d{1,2})\s+(\w{3})", re.I)
_RE_HAMQTH = re.compile(r"^(\d{2})(\d{2})\s+(\d{4})-(\d{2})-(\d{2})")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def band_of(freq_khz: float) -> str:
    """Amateur band of a frequency in kHz ("20M"), '' if outside any known band."""
    plan = [(1800, 2000, "160M"), (3500, 4000, "80M"), (5250, 5450, "60M"), (7000, 7300, "40M"),
            (10100, 10150, "30M"), (14000, 14350, "20M"), (18068, 18168, "17M"),
            (21000, 21450, "15M"), (24890, 24990, "12M"), (28000, 29700, "10M"),
            (50000, 54000, "6M"), (70000, 71000, "4M"), (144000, 148000, "2M"),
            (430000, 440000, "70CM"), (1240000, 1300000, "23CM")]
    for low, high, band in plan:
        if low <= freq_khz <= high:
            return band
    return ""


# ── Mode of a spot ─────────────────────────────────────────────────────────
# The cluster does not give the mode: it is read from the comment ("CQ LSB",
# "FT8 -06db") and, failing that, from the band plan. Only the broad family is
# kept — telegraphy, phone, digital —, the only thing useful to know whether a
# spot concerns the current traffic.

CW, PHONE, DIGI = "CW", "PHONE", "DIGI"

# band → (end of the CW segment, end of the digital segment) in kHz;
# above that, it is phone (IARU Region 1).
SEGMENTS = {
    "160M": (1838, 1843), "80M": (3570, 3600), "60M": (5354, 5366),
    "40M": (7040, 7050), "30M": (10130, 10150), "20M": (14070, 14112),
    "17M": (18095, 18111), "15M": (21070, 21151), "12M": (24915, 24931),
    "10M": (28070, 28320), "6M": (50100, 50400), "4M": (70100, 70200),
    "2M": (144110, 144180), "70CM": (432100, 432200), "23CM": (1296100, 1296200),
}
_WORDS = {
    CW: ("CW",),
    DIGI: ("FT8", "FT4", "RTTY", "PSK", "PSK31", "JS8", "JT65", "JT9", "MFSK", "OLIVIA",
           "SSTV", "DIGI", "DATA", "FST4", "Q65", "WSPR", "PACKET", "HELL"),
    PHONE: ("SSB", "LSB", "USB", "FM", "AM", "PHONE", "FONE", "VOICE", "PHONIE"),
}
_RE_WORD = re.compile(r"[A-Z0-9]+")
# Mode selected in the log → matching family.
MODE_FAMILY = {"CW": CW, "SSB": PHONE, "LSB": PHONE, "USB": PHONE, "FM": PHONE, "AM": PHONE,
               "FT8": DIGI, "FT4": DIGI, "RTTY": DIGI, "PSK31": DIGI, "PSK": DIGI,
               "SSTV": DIGI, "DIGI": DIGI, "JS8": DIGI}


def family_of_mode(mode: str) -> str:
    """Log mode ("SSB", "FT8") → family ("PHONE", "DIGI"). '' if unknown."""
    return MODE_FAMILY.get((mode or "").strip().upper(), "")


def mode_family(freq_khz: float, comment: str = "") -> str:
    """Mode family of a spot, from its comment and then from the band plan."""
    words = set(_RE_WORD.findall((comment or "").upper()))
    for family in (DIGI, CW, PHONE):        # "FT8 CW skimmer": digital wins
        if words & set(_WORDS[family]):
            return family
    limits = SEGMENTS.get(band_of(freq_khz))
    if not limits:
        return ""
    cw_end, digi_end = limits            # exclusive bounds: 7040 = RTTY start, 3600 = phone start
    if freq_khz < cw_end:
        return CW
    return DIGI if freq_khz < digi_end else PHONE


def _age_minutes(when: str) -> int | None:
    """"1433z 20 Sep" → age in minutes (None if unreadable)."""
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
    if stamp - now > _ONE_DAY:          # spot from last year (January rollover)
        stamp = stamp.replace(year=now.year - 1)
    return max(0, int((now - stamp).total_seconds() // 60))


_ONE_DAY = (datetime(2000, 1, 2, tzinfo=timezone.utc) - datetime(2000, 1, 1, tzinfo=timezone.utc))


def _from_dxwatch(call: str, limit: int) -> list[dict[str, Any]]:
    """Spots where ``call`` is the station heard.

    The parameter is ``cdx`` (the DX): ``cde`` would filter on the spotter — the
    cluster "DE" —, i.e. spots *sent by* the callsign, which is precisely what a
    special event station never does.

    Each row of the response is ``[spotter, frequency, DX, comment,
    timestamp, age in seconds, …]``.
    """
    response = httpx.get(DXWATCH_URL, params={"s": 0, "r": limit, "cdx": call},
                         timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    spots = []
    for row in (response.json().get("s") or {}).values():
        if len(row) < 5 or (row[2] or "").strip().upper() != call:
            continue
        try:
            freq = float(row[1])
        except (TypeError, ValueError):
            continue
        spots.append({"dx": call, "freq_khz": freq, "spotter": (row[0] or "").strip().upper(),
                      "comment": _clean(row[3]), "when": row[4],
                      "age_min": _age_of(row), "band": band_of(freq),
                      "mode": mode_family(freq, row[3])})
    return spots


def _clean(comment: Any) -> str:
    """Spot comment: HTML entities decoded, reasonable length."""
    text = str(comment or "")
    for entity, char in (("&gt;", ">"), ("&lt;", "<"), ("&amp;", "&"), ("&quot;", '"')):
        text = text.replace(entity, char)
    return text.strip()[:60]


def _age_of(row: list[Any]) -> int | None:
    """Spot age in minutes: DXWatch gives it in seconds (6th column)."""
    if len(row) > 5:
        try:
            return max(0, int(row[5]) // 60)
        except (TypeError, ValueError):
            pass
    return _age_minutes(row[4] if len(row) > 4 else "")


def _from_hamqth(call: str, limit: int) -> list[dict[str, Any]]:
    """Fallback: the latest worldwide spots, filtered on our callsign.

    A line reads ``DX^frequency^spotter^comment^HHMM YYYY-MM-DD^…``.
    """
    response = httpx.get(HAMQTH_URL, params={"limit": 300}, timeout=REQUEST_TIMEOUT,
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
                      "comment": _clean(parts[3]), "when": parts[4].strip(),
                      "age_min": _age_hamqth(parts[4]), "band": band_of(freq),
                      "mode": mode_family(freq, parts[3])})
    return spots[:limit]


def _age_hamqth(when: str) -> int | None:
    """"1710 2026-09-20" → age in minutes (None if unreadable)."""
    match = _RE_HAMQTH.match((when or "").strip())
    if not match:
        return None
    hour, minute, year, month, day = (int(g) for g in match.groups())
    try:
        stamp = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - stamp).total_seconds() // 60))


def recent_spots(call: str, limit: int = 5, force: bool = False) -> list[dict[str, Any]]:
    """Latest spots of this callsign, newest first ([] if there is no
    Internet or if nobody has spotted us)."""
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
        except Exception:  # noqa: BLE001 — no network, source down: move on
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
