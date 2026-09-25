"""Activation log ADIF import → one certificate data set per contacted callsign, with ranking."""

import re
from .render import normalize

_TAG = re.compile(r"<(\w+)(?::(\d+))?(?::\w)?>", re.I)


def read_adif(text: str) -> list[dict]:
    """Parses an ADIF file (header skipped). Returns a list of {lowercase_field: value} dicts."""
    i = text.lower().find("<eoh>")
    if i >= 0:
        text = text[i + 5 :]
    recs, cur, pos = [], {}, 0
    while True:
        m = _TAG.search(text, pos)
        if not m:
            break
        name, length = m.group(1).lower(), m.group(2)
        pos = m.end()
        if name == "eor":
            if cur:
                recs.append(cur)
            cur = {}
            continue
        if length:
            n = int(length)
            cur[name] = text[pos : pos + n].strip()
            pos += n
    return recs


def _qso(r):
    d, t = r.get("qso_date", ""), r.get("time_on", "")
    mode = (r.get("submode") if r.get("mode", "").upper() in ("MFSK",) else r.get("mode", "")).upper()
    if mode in ("USB", "LSB"):
        mode = "SSB"
    q = dict(
        date=f"{d[:4]}-{d[4:6]}-{d[6:8]}",
        time_utc=f"{t[:2]}:{t[2:4]}",
        freq_mhz=float(r.get("freq", 0) or 0),
        mode=mode,
        rst_sent=r.get("rst_sent", ""),
        rst_rcvd=r.get("rst_rcvd", ""),
    )
    if r.get("band"):
        q["band"] = re.sub(r"^(\d+)\s*(c?m)$", r"\1 \2", r["band"].lower())
    return q


def certificates_from_adif(text: str, config: dict, min_qso: int = 1) -> list[dict]:
    """config = {activation, scoring, certificate:{number_prefix, manager}, footnote?, names?:{CALL: name}}
    Ranking: points ↓, then QSO count ↓, then first QSO ↑ (the quickest wins)."""
    by_call = {}
    for r in read_adif(text):
        call = r.get("call", "").upper()
        if not call:
            continue
        e = by_call.setdefault(
            call, {"qso": [], "locator": r.get("gridsquare", ""), "name": r.get("name", "")}
        )
        e["qso"].append(_qso(r))
    sets = []
    for call, e in by_call.items():
        if len(e["qso"]) < min_qso:
            continue
        d = normalize(
            dict(
                activation=config["activation"],
                scoring=config.get("scoring", {}),
                recipient=dict(
                    callsign=call, locator=e["locator"], name=(config.get("names") or {}).get(call, e["name"])
                ),
                qso=e["qso"],
                certificate=dict(config.get("certificate", {})),
                footnote=config.get("footnote"),
            )
        )
        sets.append(d)
    sets.sort(
        key=lambda d: (
            -d["stats"]["points"],
            -d["stats"]["count"],
            d["qso"][0]["date"] + d["qso"][0]["time_utc"],
        )
    )
    prefix = config.get("certificate", {}).get("number_prefix", "CERT-")
    for i, d in enumerate(sets, 1):
        d["ranking"] = {"position": i, "total": len(sets)}
        d["certificate"]["number"] = f"{prefix}{i:03d}"
        d["certificate"].pop("number_prefix", None)
    return sets
