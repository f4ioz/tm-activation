"""Import ADIF du log de l'activation → un jeu de données certificat par indicatif contacté, avec classement."""
import re
from .render import normaliser

_TAG = re.compile(r"<(\w+)(?::(\d+))?(?::\w)?>", re.I)

def lire_adif(texte: str) -> list[dict]:
    """Parse un fichier ADIF (en-tête ignoré). Retourne une liste de dicts {champ_minuscule: valeur}."""
    i = texte.lower().find("<eoh>")
    if i >= 0: texte = texte[i + 5:]
    recs, cur, pos = [], {}, 0
    while True:
        m = _TAG.search(texte, pos)
        if not m: break
        nom, lg = m.group(1).lower(), m.group(2)
        pos = m.end()
        if nom == "eor":
            if cur: recs.append(cur)
            cur = {}; continue
        if lg:
            n = int(lg); cur[nom] = texte[pos:pos + n].strip(); pos += n
    return recs

def _qso(r):
    d, t = r.get("qso_date", ""), r.get("time_on", "")
    mode = (r.get("submode") if r.get("mode", "").upper() in ("MFSK",) else r.get("mode", "")).upper()
    if mode in ("USB", "LSB"): mode = "SSB"
    q = dict(date=f"{d[:4]}-{d[4:6]}-{d[6:8]}", heure_utc=f"{t[:2]}:{t[2:4]}",
             freq_mhz=float(r.get("freq", 0) or 0), mode=mode,
             rst_envoye=r.get("rst_sent", ""), rst_recu=r.get("rst_rcvd", ""))
    if r.get("band"): q["bande"] = re.sub(r"^(\d+)\s*(c?m)$", r"\1 \2", r["band"].lower())
    return q

def certificats_depuis_adif(texte: str, config: dict, qso_min: int = 1) -> list[dict]:
    """config = {activation, bareme, certificat:{prefixe_numero, gestionnaire}, mention?, noms?:{CALL: nom}}
    Classement : points ↓, puis nb QSO ↓, puis premier QSO ↑ (le plus rapide l'emporte)."""
    par_call = {}
    for r in lire_adif(texte):
        call = r.get("call", "").upper()
        if not call: continue
        e = par_call.setdefault(call, {"qso": [], "locator": r.get("gridsquare", ""), "nom": r.get("name", "")})
        e["qso"].append(_qso(r))
    jeux = []
    for call, e in par_call.items():
        if len(e["qso"]) < qso_min: continue
        d = normaliser(dict(activation=config["activation"], bareme=config.get("bareme", {}),
                            destinataire=dict(indicatif=call, locator=e["locator"],
                                              nom=(config.get("noms") or {}).get(call, e["nom"])),
                            qso=e["qso"], certificat=dict(config.get("certificat", {})),
                            mention=config.get("mention")))
        jeux.append(d)
    jeux.sort(key=lambda d: (-d["stats"]["points"], -d["stats"]["nb"],
                             d["qso"][0]["date"] + d["qso"][0]["heure_utc"]))
    pref = config.get("certificat", {}).get("prefixe_numero", "CERT-")
    for i, d in enumerate(jeux, 1):
        d["classement"] = {"position": i, "total": len(jeux)}
        d["certificat"]["numero"] = f"{pref}{i:03d}"
        d["certificat"].pop("prefixe_numero", None)
    return jeux
