"""Activation module for a temporary club callsign (e.g. TM25TEST).

Coordination of slots (who / when / band / mode) and QSO logging. The area is
reserved for club operators (dedicated operator password, ``tm_auth``
session); the site admin (private mode) can access it too. The operator at the
microphone is remembered in a ``tm_op`` cookie.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.concurrency import run_in_threadpool

from urllib.parse import quote, unquote

from starlette.convertors import Convertor, register_url_convertor

from app import activation, certificate, dx_spots, i18n, report, security, visits
from app.auth import is_private
from app.config import club_config
from app.i18n import _
from app.templating import templates

# Language of each request ("lang" cookie, otherwise the browser): see app/i18n.py.
router = APIRouter(prefix="/activation", tags=["activation"], dependencies=[Depends(i18n.request_lang)])

# Public board (outside the /activation prefix) — shareable URL, no login.
public_router = APIRouter(tags=["activation"], dependencies=[Depends(i18n.request_lang)])

COOKIE_OP = "tm_op"
COOKIE_TZ = "tm_tz"
COOKIE_TZNAME = "tm_tzname"     # time zone reported by the visitor's browser


def _tz_mode(request: Request) -> str:
    """Display time zone of the request: "utc", or an IANA time zone.

    In local mode, it is the visitor's (their browser stored it in a cookie):
    times are shown in their own local time, with no setting. Failing that — no
    JavaScript, first display — it is the station's.
    """
    mode = request.cookies.get(COOKIE_TZ) or "local"
    if mode == "utc":
        return "utc"
    return _visitor_tz(request) or "local"


def _visitor_tz(request: Request) -> str:
    """Time zone reported by the browser ("" if missing or unknown).

    The "/" is decoded in case a browser escaped it (%2F)."""
    raw = unquote((request.cookies.get(COOKIE_TZNAME) or "").strip())
    return raw if activation.valid_tz(raw) else ""


def _local_tz_label(request: Request) -> str:
    """Name of the visitor's time zone, even when they read the page in UTC."""
    return activation.tz_label(_visitor_tz(request) or "local")


def _authed(request: Request) -> bool:
    """Access granted to the TM25TEST area: club operator OR site admin."""
    return activation.operator_authed(request.cookies.get(activation.OP_COOKIE)) or is_private(request)


def _session_op(request: Request) -> str:
    """Callsign entered at login ("": session opened before 1.21, or site admin)."""
    token = request.cookies.get(activation.OP_COOKIE)
    if not activation.operator_authed(token):
        return ""
    return activation.session_operator(token)


def _is_admin(request: Request) -> bool:
    """Site admin (config.yml password) OR admin or superadmin operator.

    The callsign is not enough: a personal password is required. With the shared
    password everyone knows it — claiming to be an administrator by typing the
    right callsign must not open the "Réglages" (Settings) page.
    """
    if is_private(request):
        return True
    op = _session_op(request)
    return bool(op) and activation.per_operator_auth() and activation.operator_is_admin(op)


def _is_superadmin(request: Request) -> bool:
    """Site admin OR superadmin operator: the only ones who can open the Settings.

    Same requirement as for admin: a personal password."""
    if is_private(request):
        return True
    op = _session_op(request)
    return bool(op) and activation.per_operator_auth() and activation.operator_is_superadmin(op)


def _guard(request: Request) -> Response | None:
    """None if access is granted; otherwise a redirect to the operator login."""
    if _authed(request):
        return None
    login = f"/activation/login?next={request.url.path}"
    if request.headers.get("HX-Request"):
        resp = Response(status_code=401)
        resp.headers["HX-Redirect"] = login
        return resp
    return RedirectResponse(login, status_code=303)


def _require_admin(request: Request) -> Response | None:
    """None for an admin (site, admin or superadmin operator); otherwise /login."""
    if _is_admin(request):
        return None
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


def _require_superadmin(request: Request) -> Response | None:
    """None for a superadmin; otherwise redirects to /login.

    The Settings are reserved for the administrator and superadmin
    operators — not for plain admins or operators.
    """
    if _is_superadmin(request):
        return None
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


def _safe_next(value: str | None) -> str:
    """Redirect restricted to internal paths of the activation area."""
    return security.safe_next(value, "/activation", prefix="/activation")


def _locked_op(request: Request) -> str:
    """Callsign enforced for the session ("" = free to choose).

    An operator without the "administrateur" box ticked stays within their scope:
    they log, plan, import and export only under the callsign given at login.
    Also true with the shared password — it is then a safeguard against
    mistakes, not a barrier: anyone who knows the password can log in again
    under another callsign. Only the site admin is exempt.
    """
    op = _session_op(request)
    if not op or is_private(request) or activation.operator_is_admin(op):
        return ""
    return op


def _current_op(request: Request) -> str:
    return _locked_op(request) or (request.cookies.get(COOKIE_OP) or "").upper()


def _ctx(request: Request, **extra: object) -> dict:
    ctx = {
        "callsign": activation.callsign(),
        "label": activation.label(),
        "current_op": _current_op(request),
        "operators": activation.list_operators(),
        "bands": activation.BANDS,
        "modes": activation.MODES,
        "log_view": activation.get_log_view(),
        "my_grid": activation.my_gridsquare(),
        "is_admin": _is_admin(request),
        "is_superadmin": _is_superadmin(request),
        "site_admin": is_private(request),     # visit log: site admin only
        "session_op": _session_op(request),
        "locked_op": _locked_op(request),
        "station": activation.current_station(),
        "slot_qsos": activation.slot_qso_counts(),
    }
    mode = _tz_mode(request)
    ctx["tz_mode"] = mode
    ctx["tz_utc"] = mode == "utc"
    ctx["local_tz_label"] = _local_tz_label(request)
    ctx["tz_label"] = activation.tz_label(mode)
    ctx["to_disp"] = lambda iso, m=mode: activation.disp(iso, m)
    ctx["cdt"] = lambda q, m=mode: activation.contact_disp(q.get("qso_date", ""), q.get("time_on", ""), m)
    ctx.update(extra)
    return ctx


# ── Auth club operators ───────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def operator_login_page(request: Request, next: str = "/activation") -> Response:
    if _authed(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "activation/login.html",
        {
            "callsign": activation.callsign(),
            "label": activation.label(),
            "station": activation.current_station(),
            "next": _safe_next(next),
            "error": None,
            "site_admin": is_private(request),
            "configured": bool(activation.operator_password()) or activation.per_operator_auth(),
            "per_operator": activation.per_operator_auth(),
            "captcha": activation.make_captcha() if activation.per_operator_auth() else None,
            "password_rule": activation.password_rule(),
            "last_call": "",
        },
    )


@router.post("/login")
async def operator_login_submit(
    request: Request,
    callsign: str = Form(""),
    password: str = Form(""),
    next: str = Form("/activation"),
    captcha: str = Form(""),
    captcha_token: str = Form(""),
    website: str = Form(""),          # invisible honeypot field: filled in = bot
) -> Response:
    target = _safe_next(next)
    per_op = activation.per_operator_auth()
    expected = activation.operator_password()
    op = (callsign or "").strip().upper()
    blocked = security.login_blocked(visits.client_ip(request))
    error = None
    if blocked:
        error = _("Trop de tentatives échouées : réessaie dans 15 minutes.")
    elif per_op and not activation.check_captcha(captcha_token, captcha, website):
        error = _("Réponse à la question incorrecte : recommencez.")
    elif per_op:
        # Account created at first login; optional approval by an admin.
        error = {
            "invalid": _("Indicatif invalide"),
            "bad": _("Mot de passe incorrect"),
            "weak": activation.password_rule(),
            "pending": _("Compte en attente de validation par un administrateur."),
            "disabled": _("Compte désactivé : voir un administrateur."),
        }.get(activation.operator_login(op, password))
    elif not activation.valid_callsign(op):
        error = _("Indicatif invalide")
    elif not expected:
        error = _("Accès opérateur non configuré — voir l'admin du site")
    elif not password or password != expected:
        error = _("Mot de passe incorrect")

    if not blocked:
        visits.record_auth(request, "operator", op, error is None)
    if error is None:
        # Shared password: the callsign joins the list (in accounts mode,
        # operator_login has already done it, with its password).
        if not per_op:
            try:
                activation.add_operator(op)
            except ValueError:
                pass
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(
            activation.OP_COOKIE, activation.make_op_token(op),
            httponly=True, samesite="lax", max_age=activation.OP_TOKEN_TTL, path="/activation",
            secure=security.is_https(request),
        )
        resp.set_cookie(
            COOKIE_OP, op, max_age=30 * 86400, samesite="lax", path="/activation",
            secure=security.is_https(request),
        )
        return resp

    if not blocked:
        await asyncio.sleep(security.FAILED_LOGIN_DELAY)  # slows down password guessing
    return templates.TemplateResponse(
        request,
        "activation/login.html",
        {
            "callsign": activation.callsign(),
            "label": activation.label(),
            "station": activation.current_station(),
            "next": target,
            "error": error,
            "site_admin": is_private(request),
            "configured": bool(expected) or per_op,
            "per_operator": per_op,
            "captcha": activation.make_captcha() if per_op else None,
            "password_rule": activation.password_rule(),
            "last_call": op,
        },
        status_code=429 if blocked else 401,
    )


@router.post("/logout")
async def operator_logout() -> Response:
    resp = RedirectResponse("/activation/login", status_code=303)
    resp.delete_cookie(activation.OP_COOKIE, path="/activation")
    return resp


@router.get("/lang/{code}")
async def set_language(request: Request, code: str, next: str = "/activations") -> Response:
    """Language choice (FR | EN button in the header bar), remembered for a year.

    Not protected, like /tz: a mere display preference, also valid for the
    public pages (cookie path=/)."""
    resp = RedirectResponse(security.safe_next(next, "/activations"), status_code=303)
    if code in i18n.LANGS:
        resp.set_cookie(i18n.COOKIE, code, max_age=i18n.COOKIE_MAX_AGE, samesite="lax", path="/",
                        secure=security.is_https(request))
    return resp


@router.get("/tz")
async def set_timezone(request: Request, mode: str = "local", next: str = "/activation") -> Response:
    """Display preference Local (visitor's time zone) / UTC. Storage is in UTC.

    Not protected: it is a mere display preference (also applies to the public
    board). Cookie path=/ to cover /activation and the /<callsign> pages.
    """
    m = mode if mode in activation.TZ_MODES else "local"
    # Internal path only ("//host", "/\host"… = external redirect).
    target = security.safe_next(next, "/activation")
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(COOKIE_TZ, m, max_age=180 * 86400, samesite="lax", path="/",
                    secure=security.is_https(request))
    return resp


# ── Dashboard ──────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    if (g := _guard(request)) is not None:
        return g
    live = activation.live_slots()
    upcoming = activation.future_slots()
    return templates.TemplateResponse(
        request,
        "activation/dashboard.html",
        _ctx(
            request,
            stats=activation.stats(),
            live=live,
            upcoming=upcoming,
            past=activation.past_slots(),
            slots_max=activation.public_slots_max(),
            operator_count=len(activation.active_operators()),
            planned_count=len(live) + len(upcoming),
            recent=activation.list_contacts(limit=10),
        ),
    )


# ── Current operator ───────────────────────────────────────────────────────


@router.post("/whoami")
async def set_operator(request: Request, operator: str = Form(""), next: str = Form("/activation/log")) -> Response:
    if (g := _guard(request)) is not None:
        return g
    target = next if next.startswith("/activation") else "/activation/log"
    resp = RedirectResponse(target, status_code=303)
    cs = (operator or "").strip().upper()
    if _locked_op(request):
        return resp                      # operator locked to their callsign
    if activation.valid_callsign(cs):
        resp.set_cookie(COOKIE_OP, cs, max_age=30 * 86400, samesite="lax", path="/activation",
                        secure=security.is_https(request))
    return resp


@router.post("/operators")
async def add_operator(request: Request, callsign: str = Form(""), name: str = Form("")) -> Response:
    if (g := _guard(request)) is not None:
        return g
    try:
        activation.add_operator(callsign, name)
    except ValueError:
        pass
    return RedirectResponse("/activation/planning", status_code=303)


# ── Settings ───────────────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    return _settings_page(request)


def _settings_page(request: Request, status_code: int = 200, **extra: object) -> Response:
    """Settings page (admin); ``extra``: error / values of the callsign form."""
    last = activation.last_backup_info()
    last_str = (
        datetime.fromtimestamp(last["mtime"], activation.PARIS).strftime("%d/%m/%Y %H:%M")
        if last else None
    )
    return templates.TemplateResponse(
        request,
        "activation/settings.html",
        _ctx(
            request,
            pw_flash=request.query_params.get("pw"),
            bk_flash=request.query_params.get("bk"),
            flags_flash=request.query_params.get("fl"),
            op_configured=bool(activation.operator_password()),
            show_contacts=activation.show_contacts(),
            show_map_stats=activation.show_map_stats(),
            auto_slots=activation.auto_slots(),
            sl_flash=request.query_params.get("sl"),
            rp_flash=request.query_params.get("rp"),
            lg_flash=request.query_params.get("lg"),
            report_options=activation.get_report_options(),
            report_hunters_max=activation.REPORT_HUNTERS_MAX,
            logo=activation.logo_info(),
            logo_on_pages=activation.logo_on_pages(),
            log_view=activation.get_log_view(),
            certificate_options=activation.get_certificate_options(),
            certificate_engine=certificate.engine_ready(),
            ce_flash=request.query_params.get("ce"),
            cert_call=(request.query_params.get("cert_call") or "").strip().upper()[:12],
            cert_sample=next((h["call"] for h in activation.hunters_ranking(1)), ""),
            certificate_flags=activation.certificate_flags(),
            log_view_max=activation.LOG_VIEW_MAX,
            lv_flash=request.query_params.get("lv"),
            public_slots_max=activation.public_slots_max(),
            public_slots_cap=activation.PUBLIC_SLOTS_MAX,
            slot_lock=activation.slot_lock(),
            callbook=activation.callbook_progress(),
            per_operator_auth=activation.per_operator_auth(),
            operator_approval=activation.operator_approval(),
            password_rule_values=activation.get_password_rule(),
            password_rule_text=activation.password_rule(),
            password_len_max=activation.PASSWORD_LEN_MAX,
            password_count_max=activation.PASSWORD_COUNT_MAX,
            accounts=activation.list_operators(active_only=False),
            au_flash=request.query_params.get("au"),
            ac_flash=request.query_params.get("ac"),
            qrz_account=activation.qrz_account(),
            qz_flash=request.query_params.get("qz"),
            scoring=activation.get_scoring(),
            map_style=activation.get_map_style(),
            map_shapes=activation.MAP_SHAPES,
            mp_flash=request.query_params.get("mp"),
            scoring_summary=activation.scoring_summary(),
            sc_flash=request.query_params.get("sc"),
            my_grid=activation.my_gridsquare(),
            last_backup_str=last_str,
            backup_count=len(activation.list_backups()),
            stations=activation.list_stations(),
            visits_summary=visits.quick_summary(),
            st_flash=request.query_params.get("st"),
            **{"st_form": {"public": 1}, "st_error": None, **extra},
        ),
        status_code=status_code,
    )


@router.post("/settings/password")
async def change_operator_password(
    request: Request,
    new_password: str = Form(""),
    confirm: str = Form(""),
) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    if not new_password.strip() or new_password != confirm:
        return RedirectResponse("/activation/settings?pw=mismatch", status_code=303)
    try:
        activation.set_operator_password(new_password)
    except ValueError:
        return RedirectResponse("/activation/settings?pw=empty", status_code=303)
    return RedirectResponse("/activation/settings?pw=ok", status_code=303)


@router.post("/settings/flags")
async def change_flags(
    request: Request, show_contacts: str = Form(""), show_map_stats: str = Form(""),
    auto_slots: str = Form(""), slot_lock: str = Form(""), public_slots_max: str = Form("0"),
) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    activation.set_flag("show_contacts", bool(show_contacts))
    activation.set_flag("show_map_stats", bool(show_map_stats))
    activation.set_flag("auto_slots", bool(auto_slots))
    activation.set_flag("slot_lock", bool(slot_lock))
    activation.set_public_slots_max(public_slots_max)
    return RedirectResponse("/activation/settings?fl=ok", status_code=303)


@router.post("/settings/auth")
async def change_auth_mode(
    request: Request, per_operator: str = Form(""), approval: str = Form(""),
    min_length: str = Form(""), min_upper: str = Form(""), min_digits: str = Form(""),
    min_special: str = Form(""),
) -> Response:
    """Shared password or one password per operator (+ approval), and
    requirements for individual passwords."""
    if (g := _require_superadmin(request)) is not None:
        return g
    activation.set_flag("per_operator_auth", bool(per_operator))
    activation.set_flag("operator_approval", bool(approval))
    activation.set_password_rule({"min_length": min_length, "min_upper": min_upper,
                                  "min_digits": min_digits, "min_special": min_special})
    return RedirectResponse("/activation/settings?au=ok#comptes", status_code=303)


@router.post("/settings/operators/{call}")
async def manage_operator(
    request: Request, call: str, action: str = Form(""), password: str = Form(""),
) -> Response:
    """Operator account management (admin): approval, rights, password."""
    if (g := _require_superadmin(request)) is not None:
        return g
    cs = (call or "").strip().upper()
    me = _session_op(request)
    flash = "ok"
    try:
        if action == "approve":
            activation.approve_operator(cs)
        elif action in ("admin", "unadmin"):
            # An administrator cannot accidentally remove their own rights.
            if action == "unadmin" and cs == me:
                flash = "self"
            else:
                activation.set_operator_admin(cs, action == "admin")
        elif action in ("superadmin", "unsuperadmin"):
            if action == "unsuperadmin" and cs == me:
                flash = "self"
            else:
                activation.set_operator_superadmin(cs, action == "superadmin")
        elif action in ("enable", "disable"):
            if action == "disable" and cs == me:
                flash = "self"
            else:
                activation.set_operator_active(cs, action == "enable")
        elif action == "password":
            activation.set_operator_password_for(cs, password)
        elif action == "forget":
            activation.clear_operator_password(cs)
        else:
            flash = "unknown"
    except ValueError:
        flash = "error"
    return RedirectResponse(f"/activation/settings?ac={flash}#comptes", status_code=303)


@router.post("/settings/qrz")
async def change_qrz_account(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    action: str = Form("save"),
) -> Response:
    """QRZ.com callbook account (admin): tested against QRZ before being saved."""
    if (g := _require_superadmin(request)) is not None:
        return g
    if action == "clear":
        activation.clear_qrz_account()
        return RedirectResponse("/activation/settings?qz=cleared#qrz", status_code=303)
    username = username.strip()
    password = password or activation.own_qrz_password(username)
    if not activation.valid_qrz_username(username) or not password:
        return RedirectResponse("/activation/settings?qz=incomplete#qrz", status_code=303)
    status, detail = await run_in_threadpool(activation.check_qrz_account, username, password)
    if status == "refused":
        return RedirectResponse("/activation/settings?qz=refused#qrz", status_code=303)
    activation.set_qrz_account(username, password)
    if status == "error":
        flash = "offline"                    # QRZ unreachable: kept, checked later
    elif "non-subscriber" in detail.lower():
        flash = "nosub"                      # valid account but no XML subscription
    else:
        flash = "ok"
    return RedirectResponse(f"/activation/settings?qz={flash}#qrz", status_code=303)


@router.post("/settings/scoring")
async def change_scoring(request: Request) -> Response:
    """Ranking points rule (admin): dynamic fields per mode."""
    if (g := _require_superadmin(request)) is not None:
        return g
    form = await request.form()
    activation.set_scoring(dict(form))
    return RedirectResponse("/activation/settings?sc=ok#points", status_code=303)


@router.post("/settings/report")
async def change_report_options(
    request: Request, hours: str = Form(""), dxcc_all: str = Form(""),
    hunters: str = Form(""), sats: str = Form(""), one_page: str = Form(""),
    runs: str = Form(""),
) -> Response:
    """Content of the PDF report (optional sections)."""
    if (g := _require_superadmin(request)) is not None:
        return g
    activation.set_report_options({"hours": hours, "dxcc_all": dxcc_all, "hunters": hunters,
                                   "sats": sats, "one_page": one_page, "runs": runs})
    return RedirectResponse("/activation/settings?rp=ok#rapport", status_code=303)


@router.post("/settings/certificate")
async def change_certificate_options(
    request: Request, enabled: str = Form(""), names: str = Form(""),
    ranking: str = Form(""), mention: str = Form(""), max_qso: str = Form(""),
    appendix: str = Form(""), flag: str = Form(""), border: str = Form(""),
    border1: str = Form(""), border2: str = Form(""), border3: str = Form(""),
    emblem: str = Form(""), emblem_text: str = Form(""), ham_symbol: str = Form(""),
    qr_url: str = Form(""),
) -> Response:
    """Hunter certificates: public availability and content."""
    if (g := _require_superadmin(request)) is not None:
        return g
    activation.set_certificate_options({"enabled": enabled, "names": names,
                                        "ranking": ranking, "mention": mention,
                                        "max_qso": max_qso, "appendix": appendix,
                                        "flag": flag, "border": border,
                                        "border1": border1, "border2": border2,
                                        "border3": border3, "emblem": emblem,
                                        "emblem_text": emblem_text,
                                        "ham_symbol": ham_symbol,
                                        "qr_url": qr_url})
    return RedirectResponse("/activation/settings?ce=ok#certificats", status_code=303)


@router.post("/settings/log-view")
async def change_log_view(request: Request, photo: str = Form(""),
                          compass: str = Form("")) -> Response:
    """Sizes of the QRZ photo and of the compass on the log page."""
    if (g := _require_superadmin(request)) is not None:
        return g
    activation.set_log_view({"photo": photo, "compass": compass})
    return RedirectResponse("/activation/settings?lv=ok#log", status_code=303)


@router.post("/settings/logo")
async def upload_logo(request: Request, logo: UploadFile | None = File(None)) -> Response:
    """Club logo: site header bar, page headers and PDF report."""
    if (g := _require_superadmin(request)) is not None:
        return g
    data = await logo.read() if logo is not None else b""
    try:
        activation.set_logo(data)
    except ValueError as exc:
        return RedirectResponse(f"/activation/settings?lg={quote(str(exc))}#rapport",
                                status_code=303)
    return RedirectResponse("/activation/settings?lg=ok#rapport", status_code=303)


@router.post("/settings/logo/show")
async def toggle_logo_on_pages(request: Request, show: str = Form("")) -> Response:
    """Show or hide the logo in the page header bar (the PDF keeps it)."""
    if (g := _require_superadmin(request)) is not None:
        return g
    activation.set_flag("logo_on_pages", bool(show))
    return RedirectResponse("/activation/settings?lg=ok#rapport", status_code=303)


@router.post("/settings/logo/delete")
async def delete_logo(request: Request) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    activation.clear_logo()
    return RedirectResponse("/activation/settings?lg=gone#rapport", status_code=303)


@router.get("/report.pdf")
async def activity_report(request: Request, station: str = "") -> Response:
    """Illustrated activation report, as PDF (administrator)."""
    if (g := _require_admin(request)) is not None:
        return g
    call = (station or activation.callsign()).strip().upper()
    if activation.get_station(call) is None:
        return RedirectResponse("/activation/settings?rp=unknown", status_code=303)
    data = await run_in_threadpool(report.build_report, call)
    name = f"{activation.slugify_call(call)}-rapport.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "Cache-Control": "no-store"},
    )


@router.post("/settings/slots-from-log")
async def rebuild_slots_from_log(request: Request) -> Response:
    """Realign the slots with the log right away (admin), without waiting for a QSO."""
    if (g := _require_superadmin(request)) is not None:
        return g
    done = activation.reconcile_slots_from_log(force=True)
    return RedirectResponse(
        f"/activation/settings?sl={done['created']}-{done['extended']}", status_code=303
    )


@router.post("/settings/map")
async def change_map_style(request: Request) -> Response:
    """Colors (modes) and shapes (bands) of the public map (admin)."""
    if (g := _require_superadmin(request)) is not None:
        return g
    form = await request.form()
    activation.set_map_style(dict(form))
    return RedirectResponse("/activation/settings?mp=ok#carte", status_code=303)


# ── Special callsigns (admin) ──────────────────────────────────────────────

_STATION_FORM_FIELDS = ("label", "gridsquare", "start_date", "end_date", "badge", "subtitle", "flags", "public")


def _station_fields(form: object) -> dict:
    """Record fields from the form (missing "public" checkbox = unticked)."""
    return {k: str(form.get(k, "")) for k in _STATION_FORM_FIELDS}  # type: ignore[attr-defined]


def _station_or_404(slug: str) -> dict:
    st = activation.station_by_slug(slug)
    if st is None:
        raise HTTPException(status_code=404)
    return st


@router.post("/stations")
async def station_create(request: Request) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    form = await request.form()
    fields = _station_fields(form)
    try:
        activation.create_station(str(form.get("callsign", "")), **fields)
    except ValueError as exc:
        return _settings_page(request, status_code=400, st_error=str(exc),
                              st_form={**fields, "callsign": str(form.get("callsign", ""))})
    return RedirectResponse("/activation/settings?st=created#stations", status_code=303)


@router.get("/stations/{slug}/edit", response_class=HTMLResponse)
async def station_edit_form(request: Request, slug: str) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    st = _station_or_404(slug)
    return templates.TemplateResponse(
        request, "activation/edit_station.html", _ctx(request, st=st, form=st, error=None)
    )


@router.post("/stations/{slug}")
async def station_edit_submit(request: Request, slug: str) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    st = _station_or_404(slug)
    fields = _station_fields(await request.form())
    try:
        activation.update_station(st["callsign"], **fields)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "activation/edit_station.html",
            _ctx(request, st=st, form={**st, **fields}, error=str(exc)), status_code=400,
        )
    return RedirectResponse("/activation/settings?st=updated#stations", status_code=303)


@router.post("/stations/{slug}/current")
async def station_set_current(request: Request, slug: str) -> Response:
    """A single current callsign: the operator area switches to this one."""
    if (g := _require_superadmin(request)) is not None:
        return g
    activation.set_current_station(_station_or_404(slug)["callsign"])
    return RedirectResponse("/activation/settings?st=current#stations", status_code=303)


@router.post("/stations/{slug}/delete")
async def station_delete(request: Request, slug: str) -> Response:
    """Delete a record with no QSO (otherwise an explained refusal)."""
    if (g := _require_superadmin(request)) is not None:
        return g
    try:
        activation.delete_station(_station_or_404(slug)["callsign"])
    except ValueError as exc:
        return _settings_page(request, status_code=400, st_error=str(exc))
    return RedirectResponse("/activation/settings?st=deleted#stations", status_code=303)


@router.post("/settings/backup")
async def backup_now_route(request: Request) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    try:
        activation.backup_now()
        flash = "ok"
    except Exception:  # noqa: BLE001
        flash = "err"
    return RedirectResponse(f"/activation/settings?bk={flash}", status_code=303)


@router.get("/settings/backup.sqlite")
async def download_backup(request: Request) -> Response:
    if (g := _require_superadmin(request)) is not None:
        return g
    path = activation.backup_now()
    stamp = path.stem.replace("activation-", "")
    return FileResponse(
        path,
        media_type="application/x-sqlite3",
        filename=f"{activation.callsign().lower()}-activation-{stamp}.sqlite",
    )


# ── Slot planning ──────────────────────────────────────────────────────────


@router.get("/planning", response_class=HTMLResponse)
async def planning(request: Request) -> Response:
    if (g := _guard(request)) is not None:
        return g
    return templates.TemplateResponse(
        request,
        "activation/planning.html",
        _ctx(
            request,
            slots=activation.list_slots(),
            conflicts=activation.conflicting_slot_ids(),
            conflict_warn=request.query_params.get("warn") == "conflict",
            now_input=activation.now_input(_tz_mode(request)),
        ),
    )


# ── Import / export ADIF ───────────────────────────────────────────────────


def _band_key(band: str) -> tuple[int, str]:
    bands = activation.BANDS
    return (bands.index(band) if band in bands else len(bands), band)


def _own_contacts(request: Request) -> list[dict]:
    """QSOs visible to the session: the whole log, or only the operator's own
    QSOs when their callsign is locked."""
    contacts = activation.list_contacts()
    locked = _locked_op(request)
    return [q for q in contacts if q["operator_call"] == locked] if locked else contacts


def _adif_page(request: Request, preview: dict | None = None) -> Response:
    """ADIF page: import (with optional preview) + export of a selection."""
    contacts = _own_contacts(request)
    q = request.query_params
    flash = None
    if q.get("imported") is not None:
        flash = {"added": q.get("imported"), "skipped": q.get("skipped", "0"),
                 "invalid": q.get("invalid", "0")}
    return templates.TemplateResponse(
        request,
        "activation/adif.html",
        _ctx(
            request,
            contacts=contacts,
            groups={
                "op": sorted({c["operator_call"] for c in contacts}),
                "band": sorted({c["band"] for c in contacts}, key=_band_key),
                "mode": sorted({c["mode"] for c in contacts}),
                "day": sorted({c["qso_date"] for c in contacts}, reverse=True),
            },
            flash=flash,
            err=q.get("err"),
            preview=preview,
            import_status=activation.IMPORT_STATUS,
            import_checked=activation.IMPORT_DEFAULT_CHECKED,
        ),
    )


@router.get("/adif", response_class=HTMLResponse)
async def adif_page(request: Request) -> Response:
    if (g := _guard(request)) is not None:
        return g
    return _adif_page(request)


@router.post("/import", response_class=HTMLResponse)
async def import_preview(
    request: Request,
    operator: str = Form(""),
    op_source: str = Form("form"),
    file: UploadFile = File(...),
) -> Response:
    """Step 1: parse the file and preview the QSOs with their status
    (duplicates…). Nothing is written to the log at this stage."""
    if (g := _guard(request)) is not None:
        return g
    text = (await file.read()).decode("utf-8", errors="replace")
    locked = _locked_op(request)
    operator = locked or operator
    prefer_file = op_source == "file" and not locked
    rows = activation.analyze_adif(text, operator, prefer_file)
    counts = dict.fromkeys(activation.IMPORT_STATUS, 0)
    for r in rows:
        counts[r["status"]] += 1
    preview = {
        "token": activation.stash_import(text),
        "filename": file.filename or "fichier.adi",
        "operator": operator.strip().upper(),
        "op_source": "file" if prefer_file else "form",
        "rows": rows,
        "counts": counts,
    }
    return _adif_page(request, preview=preview)


@router.post("/import/confirm")
async def import_confirm(
    request: Request,
    token: str = Form(""),
    operator: str = Form(""),
    op_source: str = Form("form"),
    sel: list[int] = Form(default=[]),
) -> Response:
    """Step 2: import the QSOs ticked in the preview (single-use token)."""
    if (g := _guard(request)) is not None:
        return g
    text = activation.load_import(token)
    if text is None:
        return RedirectResponse("/activation/adif?err=expired", status_code=303)
    activation.drop_import(token)  # single use: no double import
    try:
        activation.backup_now()    # safety net before a bulk import
    except Exception:  # noqa: BLE001
        pass
    locked = _locked_op(request)
    rows = activation.analyze_adif(text, locked or operator, op_source == "file" and not locked)
    res = activation.import_rows(rows, set(sel))
    return RedirectResponse(
        f"/activation/adif?imported={res['added']}&skipped={res['skipped']}&invalid={res['invalid']}",
        status_code=303,
    )


@router.post("/export-selection.adi")
async def export_selection(request: Request, ids: list[int] = Form(default=[])) -> Response:
    """ADIF of only the QSOs ticked on the ADIF page."""
    if (g := _guard(request)) is not None:
        return g
    contacts = activation.contacts_by_ids(ids)
    if (locked := _locked_op(request)):
        contacts = [q for q in contacts if q["operator_call"] == locked]
    if not contacts:
        return RedirectResponse("/activation/adif?err=empty", status_code=303)
    stamp = datetime.now(activation.UTC).strftime("%Y%m%d-%H%M")
    fname = f"{activation.callsign().lower()}-selection-{stamp}.adi"
    return PlainTextResponse(
        activation.to_adif(contacts),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/slots")
async def create_slot(
    request: Request,
    operator: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    band: str = Form(""),
    mode: str = Form(""),
    note: str = Form(""),
) -> Response:
    if (g := _guard(request)) is not None:
        return g
    tzm = _tz_mode(request)
    operator = _locked_op(request) or operator      # no slot in someone else's name
    start_utc = activation.input_to_utc_iso(start, tzm)
    end_utc = activation.input_to_utc_iso(end, tzm)
    warn = ""
    if start_utc and end_utc:
        conflicts = activation.slot_conflicts(start_utc, end_utc, band)
        try:
            activation.add_slot(operator, start_utc, end_utc, band, mode, note)
            if conflicts:
                warn = "?warn=conflict"
        except ValueError:
            pass
    return RedirectResponse(f"/activation/planning{warn}", status_code=303)


def _may_touch_slot(request: Request, slot_id: int) -> bool:
    """Can the session edit/delete this slot? (everyone their own)"""
    locked = _locked_op(request)
    if not locked:
        return True
    slot = activation.get_slot(slot_id)
    return bool(slot) and slot["operator_call"] == locked


@router.get("/slots/{slot_id}/edit", response_class=HTMLResponse)
async def edit_slot_form(request: Request, slot_id: int) -> Response:
    if (g := _guard(request)) is not None:
        return g
    slot = activation.get_slot(slot_id)
    if slot is None or not _may_touch_slot(request, slot_id):
        return RedirectResponse("/activation/planning", status_code=303)
    return templates.TemplateResponse(
        request,
        "activation/edit_slot.html",
        _ctx(request, slot=slot),
    )


@router.post("/slots/{slot_id}")
async def edit_slot_submit(
    request: Request,
    slot_id: int,
    operator: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    band: str = Form(""),
    mode: str = Form(""),
    note: str = Form(""),
) -> Response:
    if (g := _guard(request)) is not None:
        return g
    if not _may_touch_slot(request, slot_id):
        return RedirectResponse("/activation/planning", status_code=303)
    tzm = _tz_mode(request)
    operator = _locked_op(request) or operator
    start_utc = activation.input_to_utc_iso(start, tzm)
    end_utc = activation.input_to_utc_iso(end, tzm)
    warn = ""
    if start_utc and end_utc:
        conflicts = activation.slot_conflicts(start_utc, end_utc, band, exclude_id=slot_id)
        try:
            activation.update_slot(slot_id, operator, start_utc, end_utc, band, mode, note)
            if conflicts:
                warn = "?warn=conflict"
        except ValueError:
            pass
    return RedirectResponse(f"/activation/planning{warn}", status_code=303)


@router.post("/slots/{slot_id}/delete")
async def remove_slot(request: Request, slot_id: int) -> Response:
    if (g := _guard(request)) is not None:
        return g
    if not _may_touch_slot(request, slot_id):
        return RedirectResponse("/activation/planning", status_code=303)
    activation.delete_slot(slot_id)
    return RedirectResponse("/activation/planning", status_code=303)


# ── Log QSO ────────────────────────────────────────────────────────────────


@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request) -> Response:
    if (g := _guard(request)) is not None:
        return g
    return templates.TemplateResponse(
        request,
        "activation/log.html",
        _ctx(
            request,
            live=activation.live_slots(),
            upcoming=activation.future_slots()[:3],
            contacts=activation.list_contacts(limit=100),
            stats=activation.stats(),
            entities=activation.worked_entities(),
            now_input=activation.now_input(_tz_mode(request)),
        ),
    )


@router.get("/qrz")
async def qrz_lookup_route(request: Request, call: str = "") -> Response:
    """QRZ lookup for log entry: first name, last name, locator, DXCC country.

    Reserved for the operator area (the site's subscribed QRZ account, a quota
    not to be opened to the public). The result is stored in the callbook.
    """
    if (g := _guard(request)) is not None:
        return g
    cs = (call or "").strip().upper()
    if not activation.valid_callsign(cs):
        return JSONResponse({"call": cs, "found": False}, status_code=400)
    client = activation.qrz_client()
    row = await run_in_threadpool(activation.qrz_lookup, cs, client)
    if row is None:
        return JSONResponse({"call": cs, "found": False, "configured": client is not None})
    home = activation.my_gridsquare()
    grid = row["grid"] or ""
    return JSONResponse(
        {
            "call": cs, "found": True,
            "fname": row["fname"], "name": row["name"], "grid": grid,
            "country": row["dxcc_name"] or row["country"],
            "image": row["image"] if "image" in row.keys() else "",
            "distance_km": activation.distance_km(home, grid),
            "bearing": activation.bearing_deg(home, grid),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/worked")
async def worked_route(request: Request, call: str = "") -> Response:
    """Station already worked? Queried while the callsign is being typed."""
    if (g := _guard(request)) is not None:
        return g
    return JSONResponse(activation.worked_before(call), headers={"Cache-Control": "no-store"})


@router.get("/slot-conflict")
async def slot_conflict_route(request: Request, band: str = "", mode: str = "") -> Response:
    """Band/mode currently reserved by another operator? (live warning)"""
    if (g := _guard(request)) is not None:
        return g
    slot = activation.blocking_slot(_current_op(request), band, mode) if activation.slot_lock() else None
    if slot is None:
        return JSONResponse({"blocked": False}, headers={"Cache-Control": "no-store"})
    end = activation.disp(slot["end_utc"], _tz_mode(request))
    return JSONResponse(
        {"blocked": True, "operator": slot["operator_call"], "band": slot["band"], "mode": slot["mode"],
         "until": end.strftime("%H:%M") if end else slot["end_utc"],
         "tz": activation.tz_label(_tz_mode(request))},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/rate", response_class=HTMLResponse)
async def rate_panel(request: Request) -> Response:
    """Rate gauges of the operator at the microphone (or of the station)."""
    if (g := _guard(request)) is not None:
        return g
    return templates.TemplateResponse(
        request,
        "activation/partials/rate.html",
        {"rate": activation.qso_rate(_current_op(request))},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/spots", response_class=HTMLResponse)
async def spots_panel(request: Request, band: str = "", mode: str = "") -> Response:
    """The log's "Am I spotted?" panel (refreshed by htmx, never blocking).

    Only spots on the current band AND traffic type (CW, phone, digital) are
    shown — that is where the pile-up happens. Without Internet, the list comes
    back empty and the panel disappears.
    """
    if (g := _guard(request)) is not None:
        return g
    call = activation.callsign()
    found = await run_in_threadpool(dx_spots.recent_spots, call, 30)
    wanted = (band or "").strip().upper()
    if wanted:
        found = [spot for spot in found if spot["band"] == wanted]
    family = dx_spots.family_of_mode(mode)
    if family:
        # A spot whose mode stays undetermined is kept: better to show it than
        # to suggest that nobody hears us.
        found = [spot for spot in found if spot["mode"] in ("", family)]
    found = found[:3]
    return templates.TemplateResponse(
        request,
        "activation/partials/spots.html",
        {"call": call, "spots": found},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/contacts", response_class=HTMLResponse)
async def create_contact(
    request: Request,
    call: str = Form(""),
    band: str = Form(""),
    mode: str = Form(""),
    operator: str = Form(""),
    rst_sent: str = Form(""),
    rst_rcvd: str = Form(""),
    freq: str = Form(""),
    gridsquare: str = Form(""),
    sat_name: str = Form(""),
    when: str = Form(""),
    now: str = Form(""),
    comment: str = Form(""),
) -> Response:
    if (g := _guard(request)) is not None:
        return g
    op = (_locked_op(request) or operator or _current_op(request)).strip().upper()
    dupe = activation.is_dupe(call, band, mode) if activation.valid_callsign(call) else False
    error = None
    try:
        freq_mhz = float(freq.replace(",", ".")) if freq.strip() else None
    except ValueError:
        freq_mhz = None
    # "Maintenant" (Now) ticked (or empty field) → submission time, in UTC;
    # otherwise the date/time entered in the current display time zone.
    qso_date = time_on = None
    if when.strip() and not now:
        iso = activation.input_to_utc_iso(when, _tz_mode(request))
        parts = activation.utc_iso_to_parts(iso) if iso else None
        if parts:
            qso_date, time_on = parts
    # Band and mode reserved by another operator right now: nothing is written
    # (two stations under the same callsign would interfere with each other).
    blocking = None
    if activation.slot_lock():
        when_utc = (activation.parts_to_utc_iso(qso_date, time_on)
                    if qso_date and time_on else None)
        blocking = activation.blocking_slot(op, band, mode, when_utc)
    if blocking is not None:
        end = activation.disp(blocking["end_utc"], _tz_mode(request))
        error = _("{band} {mode} est réservé par {call} jusqu'à {end} ({tz}) : QSO non enregistré.",
                  band=blocking["band"], mode=blocking["mode"], call=blocking["operator_call"],
                  end=end.strftime("%H:%M") if end else blocking["end_utc"],
                  tz=activation.tz_label(_tz_mode(request)))
    else:
        try:
            activation.add_contact(
                call=call, band=band, mode=mode, operator_call=op,
                qso_date=qso_date, time_on=time_on,
                freq_mhz=freq_mhz, rst_sent=rst_sent, rst_rcvd=rst_rcvd,
                gridsquare=gridsquare, sat_name=sat_name, comment=comment,
            )
        except ValueError as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request,
        "activation/partials/log_result.html",
        {
            "contacts": activation.list_contacts(limit=100),
            "stats": activation.stats(),
            "entities": activation.worked_entities(),
            "dupe": dupe,
            "error": error,
            "last_call": (call or "").strip().upper(),
        },
    )


def _may_touch(request: Request, contact_id: int) -> bool:
    """Can the session edit/delete this QSO? (everyone their own QSOs)"""
    locked = _locked_op(request)
    if not locked:
        return True
    contacts = activation.contacts_by_ids([contact_id])
    return bool(contacts) and contacts[0]["operator_call"] == locked


@router.post("/contacts/{contact_id}/delete", response_class=HTMLResponse)
async def remove_contact(request: Request, contact_id: int) -> Response:
    if (g := _guard(request)) is not None:
        return g
    if not _may_touch(request, contact_id):
        return templates.TemplateResponse(
            request,
            "activation/partials/log_result.html",
            {
                "contacts": activation.list_contacts(limit=100),
                "stats": activation.stats(),
                "entities": activation.worked_entities(),
                "dupe": False,
                "error": _("Ce QSO est celui d'un autre opérateur : demandez à un administrateur."),
                "last_call": "",
            },
        )
    activation.delete_contact(contact_id)
    return templates.TemplateResponse(
        request,
        "activation/partials/log_result.html",
        {
            "contacts": activation.list_contacts(limit=100),
            "stats": activation.stats(),
            "entities": activation.worked_entities(),
            "dupe": False,
            "error": None,
            "last_call": "",
        },
    )


@router.get("/contacts/{contact_id}/edit", response_class=HTMLResponse)
async def edit_contact_form(request: Request, contact_id: int) -> Response:
    if (g := _guard(request)) is not None:
        return g
    if not _may_touch(request, contact_id):
        return RedirectResponse("/activation/log", status_code=303)
    contact = activation.get_contact(contact_id)
    if contact is None:
        return RedirectResponse("/activation/log", status_code=303)
    return templates.TemplateResponse(
        request,
        "activation/edit_contact.html",
        _ctx(request, contact=contact),
    )


@router.post("/contacts/{contact_id}")
async def update_contact(
    request: Request,
    contact_id: int,
    call: str = Form(""),
    band: str = Form(""),
    mode: str = Form(""),
    operator: str = Form(""),
    qso_date: str = Form(""),
    time_on: str = Form(""),
    rst_sent: str = Form(""),
    rst_rcvd: str = Form(""),
    freq: str = Form(""),
    gridsquare: str = Form(""),
    sat_name: str = Form(""),
    comment: str = Form(""),
) -> Response:
    if (g := _guard(request)) is not None:
        return g
    if not _may_touch(request, contact_id):
        return RedirectResponse("/activation/log", status_code=303)
    operator = _locked_op(request) or operator      # no QSO in someone else's name
    try:
        freq_mhz = float(freq.replace(",", ".")) if freq.strip() else None
    except ValueError:
        freq_mhz = None
    try:
        activation.update_contact(
            contact_id,
            call=call, band=band, mode=mode, operator_call=operator,
            qso_date=qso_date.strip(), time_on=time_on.strip()[:4],
            freq_mhz=freq_mhz, rst_sent=rst_sent, rst_rcvd=rst_rcvd,
            gridsquare=gridsquare, sat_name=sat_name, comment=comment,
        )
    except ValueError:
        pass
    return RedirectResponse("/activation/log", status_code=303)


# ── Exports ────────────────────────────────────────────────────────────────


@router.get("/export.adi")
async def export_adif(request: Request) -> Response:
    if (g := _guard(request)) is not None:
        return g
    body = activation.to_adif(_own_contacts(request))
    fname = f"{activation.callsign().lower()}.adi"
    return PlainTextResponse(
        body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export.csv")
async def export_csv(request: Request) -> Response:
    if (g := _guard(request)) is not None:
        return g
    body = activation.to_csv(_own_contacts(request))
    fname = f"{activation.callsign().lower()}.csv"
    return PlainTextResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Public board (read-only) ───────────────────────────────────────────────


def _public_stats(station: str, search_call: str = "") -> dict:
    """Map / DXCC / ranking of a callsign, if the setting allows it.
    ``search_rank``: position of the searched callsign in the ranking."""
    if not activation.show_map_stats():
        return {"show_map_stats": False}
    grid = activation.my_gridsquare(station)
    ranking = activation.hunters_ranking(None, station)
    rule = activation.get_scoring(station)
    return {
        "show_map_stats": True,
        "map_data": activation.map_data(station),
        "map_style": activation.get_map_style(station),
        "dxcc": activation.dxcc_table(station),
        "ranking": ranking[:50],
        "search_rank": next((h for h in ranking if h["call"] == search_call), None),
        "scoring": rule,
        "scoring_summary": activation.scoring_summary(rule),
        "home": {"call": station, "grid": grid, "pos": activation.locator_center(grid)},
    }


@public_router.get("/logo")
async def club_logo(request: Request) -> Response:
    """Club logo, uploaded in the Settings (404 as long as there is none)."""
    info = activation.logo_info()
    if info is None:
        return Response(status_code=404)
    return FileResponse(
        info["path"], media_type=info["media_type"],
        headers={"Cache-Control": "public, max-age=3600"},
    )


@public_router.get("/activations", response_class=HTMLResponse)
async def stations_page(request: Request) -> Response:
    """Public list of the club's special callsigns (published pages)."""
    return templates.TemplateResponse(
        request,
        "activation/stations.html",
        {
            "site_admin": is_private(request),
            "callsign": club_config().get("callsign") or activation.callsign(),
            "label": club_config().get("name") or _("Indicatifs spéciaux"),
            "stations": [
                {**s, "status": activation.station_status(s)}
                for s in activation.list_stations() if s["public"]
            ],
        },
    )


_RE_SLUG = re.compile(r"^[a-z0-9]{3,16}$")


class _CallSlugConvertor(Convertor):
    """Callsign segment: letters/digits with at least one digit. That way the
    generic route catches neither /.git, /.ssh… nor /content (no more 307)."""

    regex = "[A-Za-z0-9]*[0-9][A-Za-z0-9]*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("callslug", _CallSlugConvertor())


@public_router.get("/{slug:callslug}/certificat")
async def hunter_certificate(request: Request, slug: str, call: str = "",
                             back: str = "") -> Response:
    """PDF certificate of a hunter (public page of the callsign).

    Open to hunters once the admin has enabled certificates; an administrator
    can always generate it, if only to proofread it before opening it to the
    public.
    """
    st = activation.station_by_slug(slug)
    if st is None:
        return Response(status_code=404)
    admin = is_private(request)
    if not admin and (not st["public"] or not activation.certificates_on()):
        return Response(status_code=404)
    cs = (call or "").strip().upper()
    # Test run from the Settings: go back there (with the message) rather than
    # sending the administrator to the public page.
    settings_back = back == "settings" and admin

    def failed(reason: str) -> Response:
        if settings_back:
            return RedirectResponse(f"/activation/settings?ce={reason}&cert_call={quote(cs)}"
                                    "#certificats", status_code=303)
        return RedirectResponse(f"/{slug}?call={quote(cs)}&cert={reason}", status_code=303)

    try:
        data = await run_in_threadpool(certificate.build_certificate, cs, st["callsign"])
    except certificate.CertificateUnavailable as exc:
        logging.getLogger(__name__).warning("certificat indisponible : %s", exc)
        return failed("moteur")
    if data is None:
        return failed("absent")
    name = f"certificat-{activation.slugify_call(st['callsign'])}-{activation.slugify_call(cs)}.pdf"
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "Cache-Control": "no-store"},
    )


@public_router.get("/{slug:callslug}", response_class=HTMLResponse)
async def public_board(request: Request, slug: str, call: str = "") -> Response:
    """Public page of a special callsign: /tm25test, /tm61xyz…

    Generic one-segment route, included last (main.py): anything that is not a
    published callsign returns 404, as before.
    """
    low = slug.lower()
    st = activation.station_by_slug(low) if _RE_SLUG.match(low) else None
    if st is None or not st["public"]:
        raise HTTPException(status_code=404)
    if slug != low:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/{low}{query}", status_code=301)
    cs = st["callsign"]
    search = activation.contacts_for_call(call, cs) if call.strip() else None
    mode = _tz_mode(request)
    return templates.TemplateResponse(
        request,
        "activation/public.html",
        {
            "station": st,
            "site_admin": is_private(request),
            "slug": st["slug"],
            "callsign": cs,
            "label": st["label"] or cs,
            "status": activation.station_status(st),
            "stats": activation.stats(cs),
            "slots": activation.future_slots(cs),
            "live": activation.live_slots(cs),
            "past": activation.past_slots(cs),
            "slot_qsos": activation.slot_qso_counts(cs),
            "slots_max": activation.public_slots_max(),
            "recent": activation.list_contacts(limit=50, station=cs),
            "search": search,
            "search_call": call.strip().upper(),
            "show_contacts": activation.show_contacts(),
            "certificates": activation.certificates_on(),
            "cert_flash": request.query_params.get("cert"),
            **_public_stats(cs, call.strip().upper()),
            "tz_mode": mode,
            "tz_utc": mode == "utc",
            "tz_label": activation.tz_label(mode),
            "local_tz_label": _local_tz_label(request),
            "to_disp": lambda iso, m=mode: activation.disp(iso, m),
            "cdt": lambda q, m=mode: activation.contact_disp(q.get("qso_date", ""), q.get("time_on", ""), m),
        },
    )
