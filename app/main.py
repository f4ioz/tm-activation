"""Point d'entrée FastAPI : application autonome d'activation d'indicatifs spéciaux.

Un seul worker uvicorn : blocages anti-bruteforce en mémoire et tâche de fond
QRZ (un thread) supposent un processus unique.
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
    """Au démarrage : snapshot de la base (best-effort) et enrichissement QRZ
    en tâche de fond ; arrêté à l'extinction."""
    try:
        activation.backup_now()
    except Exception:  # noqa: BLE001 — ne jamais bloquer le démarrage
        pass
    try:
        activation.start_enricher()
    except Exception:  # noqa: BLE001
        pass
    yield
    activation.stop_enricher()


# Pas de /docs, /redoc, /openapi.json : inutile de publier le plan des routes.
app = FastAPI(title="TM Activation", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def shield_middleware(request: Request, call_next):
    """Blocage temporaire des scanners + en-têtes de sécurité (security.py)."""
    blocked = security.check_request(request)
    if blocked is not None:
        return blocked
    response = await call_next(request)
    security.add_headers(request, response)
    return response


# Ajouté en dernier = exécuté en premier : l'IP réelle (X-Forwarded-For) et le
# schéma https (X-Forwarded-Proto) ne sont repris QUE des proxys de confiance.
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=server_config().get("trusted_proxies") or ["127.0.0.1", "::1"],
)

app.include_router(site_router.router)
app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(activation_router.router)
# En dernier : pages publiques /<indicatif> (route générique à un segment).
app.include_router(activation_router.public_router)
