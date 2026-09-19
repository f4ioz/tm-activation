"""Génère config.yml à partir de config.yml.example et des réponses d'install.sh.

    python make_config.py config.yml.example config.yml

Les valeurs arrivent par variables d'environnement TMCFG_* (pas en argument :
les mots de passe n'apparaissent pas dans la liste des processus). Refuse
d'écraser un config.yml existant.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

import yaml


def env(name: str, default: str = "") -> str:
    return os.environ.get(f"TMCFG_{name}", default).strip()


def main(src: str, dst: str) -> int:
    with open(src, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault("site", {})["base_url"] = env("BASE_URL")
    proxies = [p for p in re.split(r"[\s,]+", env("TRUSTED_PROXIES")) if p]
    if proxies:
        cfg.setdefault("server", {})["trusted_proxies"] = proxies
    cfg.setdefault("auth", {})["password"] = env("ADMIN_PASSWORD")
    cfg["club"] = {
        "callsign": env("CLUB_CALLSIGN").upper(),
        "name": env("CLUB_NAME"),
        "city": env("CLUB_CITY"),
        "website": env("CLUB_WEBSITE"),
    }
    qrz = cfg.setdefault("qrz", {})
    qrz["username"] = env("QRZ_USER")
    qrz["password"] = env("QRZ_PASSWORD")
    act = cfg.setdefault("activation", {})
    act["callsign"] = env("CALLSIGN").upper()
    act["label"] = env("LABEL")
    act["my_gridsquare"] = env("GRID").upper()
    act["public"] = env("PUBLIC", "0").lower() in ("1", "o", "oui", "y", "yes", "true")
    act["password"] = env("OPERATOR_PASSWORD")
    act["operators"] = [c.upper() for c in re.split(r"[\s,;]+", env("OPERATORS")) if c]

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"# TM Activation — généré par install.sh le {stamp}.\n"
        "# Rôle de chaque clé : voir config.yml.example. Après modification :\n"
        "#   sudo systemctl restart tm-activation\n\n"
    )
    body = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(header + body)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
