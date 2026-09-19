"""Connexion administrateur : /login (formulaire), /logout."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import activation, auth as auth_mod, security, visits
from app.templating import templates

router = APIRouter(tags=["auth"])

DEFAULT_NEXT = "/activation/settings"


def _safe_next(value: str | None) -> str:
    """Restreint la redirection à des chemins relatifs internes."""
    return security.safe_next(value, DEFAULT_NEXT)


def _page(request: Request, next_: str, error: str | None, status_code: int = 200) -> Response:
    station = activation.current_station()
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "callsign": station["callsign"],
            "label": station["label"] or station["callsign"],
            "station": station,
            "next": next_,
            "error": error,
            "configured": auth_mod.auth_enabled(),
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = DEFAULT_NEXT) -> Response:
    if auth_mod.is_private(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _page(request, _safe_next(next), None)


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form(DEFAULT_NEXT),
) -> Response:
    target = _safe_next(next)
    if security.login_blocked(visits.client_ip(request)):
        return _page(request, target, "Trop de tentatives échouées : réessaie dans 15 minutes.", 429)
    expected = auth_mod.auth_password()
    ok = bool(expected and password and password == expected)
    visits.record_auth(request, "admin", "", ok)
    if ok:
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(
            auth_mod.COOKIE_NAME,
            auth_mod.make_token(),
            httponly=True,
            samesite="lax",
            secure=security.is_https(request),
            max_age=auth_mod.TOKEN_TTL,
            path="/",
        )
        return resp
    await asyncio.sleep(security.FAILED_LOGIN_DELAY)  # ralentit les essais de mots de passe
    return _page(request, target, "Mot de passe incorrect" if expected else "Administration non configurée", 401)


@router.post("/logout")
async def logout(next: str = Form("/")) -> Response:
    resp = RedirectResponse(security.safe_next(next, "/"), status_code=303)
    resp.delete_cookie(auth_mod.COOKIE_NAME, path="/")
    return resp
