"""Instance Jinja2Templates partagée + globals communs aux templates."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import activation, dxcc_flags, i18n
from app.config import club_config

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def asset(path: str) -> str:
    """URL d'un asset statique suffixée d'un cache-buster ?v=<mtime>.

    Évite que le navigateur (ou nginx) serve un JS/CSS périmé après une mise
    à jour : le suffixe change automatiquement à chaque modification du fichier."""
    rel = path[len("/static/"):] if path.startswith("/static/") else path.lstrip("/")
    try:
        v = int((STATIC_DIR / rel).stat().st_mtime)
    except OSError:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}v={v}"


_FR_DOW = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")


def dow_fr(dt) -> str:
    """Jour de la semaine abrégé en français ('lun'…'dim'). '' si invalide."""
    try:
        return _FR_DOW[dt.weekday()]
    except (AttributeError, IndexError, TypeError):
        return ""


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["club"] = {k: str(v or "") for k, v in club_config().items()}
templates.env.globals["asset"] = asset
templates.env.globals["dow_fr"] = dow_fr
templates.env.globals["flag_prefixes"] = activation.FLAG_PREFIXES
templates.env.globals["note_is_sat"] = activation.note_is_sat
# Logo du club (Réglages) : 0 s'il n'y en a pas, sinon la date du fichier, qui
# sert aussi de numéro de version pour le cache du navigateur.
templates.env.globals["logo_version"] = lambda: (activation.logo_info() or {}).get("mtime", 0)
templates.env.globals["dxcc_entity"] = dxcc_flags.entity_for_call
templates.env.globals["dxcc_flag_file"] = dxcc_flags.flag_file
i18n.install(templates.env)
