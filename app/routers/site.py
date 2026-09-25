"""Site root, robots.txt and health probe."""

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
    """Public page of the current callsign if it is online, else the list."""
    st = activation.current_station()
    return RedirectResponse(f"/{st['slug']}" if st["public"] else "/activations", status_code=302)


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots() -> str:
    return ROBOTS


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Probe for install.sh / monitoring: the app answers and can read its database."""
    return JSONResponse({"status": "ok", "version": version(), "station": activation.callsign()},
                        headers={"Cache-Control": "no-store"})
