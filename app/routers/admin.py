"""Monitoring: logins and blocked bots (site administrator)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import auth as auth_mod, i18n, security, visits
from app.templating import templates

router = APIRouter(tags=["admin"], dependencies=[Depends(i18n.request_lang)])


@router.get("/admin/surveillance", response_class=HTMLResponse)
async def surveillance(request: Request, days: int = 7, failures: str = "") -> Response:
    """Login log and current blocks. Administrator only."""
    if not auth_mod.is_private(request):
        return RedirectResponse("/login?next=/admin/surveillance", status_code=303)
    days = days if days in (1, 7, 30, 90) else 7
    return templates.TemplateResponse(
        request,
        "admin/surveillance.html",
        {
            "site_admin": True,
            "callsign": "",
            "days": days,
            "failures_only": bool(failures),
            "counts": {period: visits.auth_counts(period) for period in (1, 7, 30)},
            "events": visits.auth_log(days=days, failures_only=bool(failures)),
            "worst_ips": visits.failed_by_ip(days=days),
            "banned": security.banned_ips(),
            "retention": visits.RETENTION_DAYS,
            "login_max": security.LOGIN_MAX_FAILURES,
            "login_window_min": security.LOGIN_WINDOW // 60,
            "scan_threshold": security.SCAN_THRESHOLD,
            "scan_window_min": security.SCAN_WINDOW // 60,
            "ban_minutes": security.BAN_SECONDS // 60,
        },
    )
