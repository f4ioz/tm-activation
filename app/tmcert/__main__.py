"""CLI :
  python -m tmcert donnees.json -o certificat.pdf
  python -m tmcert --adif log.adi --config activation.json -o dossier/"""
import argparse, json, os, sys
from . import render, certificats_depuis_adif

ap = argparse.ArgumentParser(prog="tmcert")
ap.add_argument("json", nargs="?"); ap.add_argument("--adif"); ap.add_argument("--config")
ap.add_argument("-o", "--sortie", required=True)
a = ap.parse_args()
if a.adif:
    cfg = json.load(open(a.config, encoding="utf-8"))
    os.makedirs(a.sortie, exist_ok=True)
    for d in certificats_depuis_adif(open(a.adif, encoding="utf-8", errors="replace").read(), cfg):
        p = os.path.join(a.sortie, f"{d['classement']['position']:03d}_{d['destinataire']['indicatif'].replace('/', '-')}.pdf")
        open(p, "wb").write(render(d)); print(p)
elif a.json:
    open(a.sortie, "wb").write(render(json.load(open(a.json, encoding="utf-8")))); print(a.sortie)
else:
    ap.error("donner un JSON ou --adif + --config")
