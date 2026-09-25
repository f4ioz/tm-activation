"""FastAPI entry point: standalone special-callsign activation application.

A single uvicorn worker: in-memory anti-bruteforce blocking and the QRZ
background task (a thread) assume a single process.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import activation, security
from app.config import server_config
from app.routers import activation as activation_router
from app.routers import admin as admin_router
from app.routers import auth as auth_router
from app.routers import site as site_router

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup: database snapshot (best-effort) and QRZ enrichment
    in the background; stopped on shutdown."""
    try:
        activation.backup_now()
    except Exception:  # noqa: BLE001 — never block startup
        pass
    try:
        activation.start_enricher()
    except Exception:  # noqa: BLE001
        pass
    yield
    activation.stop_enricher()


# No /docs, /redoc, /openapi.json: no point publishing the route map.
app = FastAPI(title="TM Activation", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def shield_middleware(request: Request, call_next):
    """Temporary scanner blocking + security headers (security.py)."""
    blocked = security.check_request(request)
    if blocked is not None:
        return blocked
    response = await call_next(request)
    security.add_headers(request, response)
    return response


# Added last = runs first: the real IP (X-Forwarded-For) and the https
# scheme (X-Forwarded-Proto) are taken ONLY from trusted proxies.
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=server_config().get("trusted_proxies") or ["127.0.0.1", "::1"],
)

app.include_router(site_router.router)
app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(activation_router.router)
# Last: public /<callsign> pages (generic single-segment route).
app.include_router(activation_router.public_router)
