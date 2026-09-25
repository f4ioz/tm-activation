"""Command line (see SPEC.md):

python -m app.tmcert data.json -o certificate.pdf
python -m app.tmcert --adif log.adi --config activation.json -o folder/
"""

import argparse
import json
import os
from . import render, certificates_from_adif

ap = argparse.ArgumentParser(prog="tmcert")
ap.add_argument("json", nargs="?")
ap.add_argument("--adif")
ap.add_argument("--config")
ap.add_argument("-o", "--output", required=True)
a = ap.parse_args()
if a.adif:
    cfg = json.load(open(a.config, encoding="utf-8"))
    os.makedirs(a.output, exist_ok=True)
    for d in certificates_from_adif(open(a.adif, encoding="utf-8", errors="replace").read(), cfg):
        p = os.path.join(
            a.output, f"{d['ranking']['position']:03d}_{d['recipient']['callsign'].replace('/', '-')}.pdf"
        )
        open(p, "wb").write(render(d))
        print(p)
elif a.json:
    open(a.output, "wb").write(render(json.load(open(a.json, encoding="utf-8"))))
    print(a.output)
else:
    ap.error("give a JSON file or --adif + --config")
