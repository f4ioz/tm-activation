"""Racine du site, robots.txt et sonde de santé."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from app import activation

router = APIRouter(tags=["site"])

VERSION_FILE = Path(__file__).resolve().parent.parent.parent / "VERSION"

ROBOTS = "User-agent: *\nDisallow: /activation/\nDisallow: /login\n"


def version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"


@router.get("/")
async def home() -> RedirectResponse:
    """Page publique de l'indicatif en cours si elle est en ligne, sinon la liste."""
    st = activation.current_station()
    return RedirectResponse(f"/{st['slug']}" if st["public"] else "/activations", status_code=302)


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots() -> str:
    return ROBOTS


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Sonde pour install.sh / supervision : l'application répond et lit sa base."""
    return JSONResponse({"status": "ok", "version": version(), "station": activation.callsign()},
                        headers={"Cache-Control": "no-store"})
