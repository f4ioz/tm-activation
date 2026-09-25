"""Shared Jinja2Templates instance + globals common to all templates."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import activation, dxcc_flags, i18n
from app.config import club_config

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def asset(path: str) -> str:
    """Static asset URL with a ?v=<mtime> cache-buster suffix.

    Keeps the browser (or nginx) from serving stale JS/CSS after an
    update: the suffix changes automatically whenever the file is modified."""
    rel = path[len("/static/"):] if path.startswith("/static/") else path.lstrip("/")
    try:
        v = int((STATIC_DIR / rel).stat().st_mtime)
    except OSError:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}v={v}"


_FR_DOW = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")


def dow_fr(dt) -> str:
    """Abbreviated French weekday name ('lun'…'dim'). '' if invalid."""
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
# Club logo (Settings): 0 if there is none, else the file's mtime, which
# also serves as a version number for the browser cache.
templates.env.globals["logo_version"] = lambda: (
    (activation.logo_info() or {}).get("mtime", 0) if activation.logo_on_pages() else 0
)
templates.env.globals["dxcc_entity"] = dxcc_flags.entity_for_call
templates.env.globals["dxcc_flag_file"] = dxcc_flags.flag_file
i18n.install(templates.env)
