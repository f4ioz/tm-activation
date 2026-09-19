"""Parseur ADIF.

Nom de module conservé pour compatibilité avec ``app/activation.py``, partagé
avec le site d'origine où ce parseur vit dans le client Wavelog. Seul le
parseur est repris ici (import ADIF de l'espace opérateurs).
"""

from __future__ import annotations

import re

_FIELD_RE = re.compile(r"<([A-Z0-9_]+):(\d+)(?::[^>]*)?>", re.IGNORECASE)
_EOR_RE = re.compile(r"<eor>", re.IGNORECASE)
_EOH_RE = re.compile(r"<eoh>", re.IGNORECASE)


def _strip_header(adif: str) -> str:
    m = _EOH_RE.search(adif)
    return adif[m.end() :] if m else adif


def parse_adif(text: str) -> list[dict[str, str]]:
    """Parse un blob ADIF en liste de dicts (clés en minuscules)."""
    body = _strip_header(text)
    records: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    i = 0
    n = len(body)
    while i < n:
        m = _FIELD_RE.search(body, i)
        eor = _EOR_RE.search(body, i)
        if not m and not eor:
            break
        if eor and (not m or eor.start() < m.start()):
            if cur:
                records.append(cur)
                cur = {}
            i = eor.end()
            continue
        assert m is not None  # for type-checker
        name = m.group(1).lower()
        length = int(m.group(2))
        value_start = m.end()
        value = body[value_start : value_start + length]
        cur[name] = value.strip()
        i = value_start + length
    if cur:
        records.append(cur)
    return records
