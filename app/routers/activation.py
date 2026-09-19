"""Module d'activation d'un indicatif temporaire de club (ex. TM25TEST).

Coordination des créneaux (qui / quand / bande / mode) et journalisation des
QSO. L'espace est réservé aux opérateurs du club (mot de passe opérateur dédié,
session ``tm_auth``) ; l'admin site (mode privé) y accède aussi. L'opérateur
au micro est mémorisé dans un cookie ``tm_op``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.concurrency import run_in_threadpool

from starlette.convertors import Convertor, register_url_convertor

from app import activation, security, visits
from app.auth import is_private
from app.config import club_config
from app.templating import templates

router = APIRouter(prefix="/activation", tags=["activation"])

# Board public (hors préfixe /activation) — URL partageable sans login.
public_router = APIRouter(tags=["activation"])

COOKIE_OP = "tm_op"
COOKIE_TZ = "tm_tz"


def _tz_mode(request: Request) -> str:
    mode = request.cookies.get(COOKIE_TZ) or "local"
    return mode if mode in activation.TZ_MODES else "local"


def _authed(request: Request) -> bool:
    """Accès autorisé à l'espace TM25TEST : opérateur du club OU admin site."""
    return activation.operator_authed(request.cookies.get(activation.OP_COOKIE)) or is_private(request)


def _guard(request: Request) -> Response | None:
    """None si l'accès est autorisé ; sinon une redirection vers le login opérateur."""
    if _authed(request):
        return None
    login = f"/activation/login?next={request.url.path}"
    if request.headers.get("HX-Request"):
        resp = Response(status_code=401)
        resp.headers["HX-Redirect"] = login
        return resp
    return RedirectResponse(login, status_code=303)


def _require_admin(request: Request) -> Response | None:
    """None si l'admin site est connecté (mode privé) ; sinon redirige vers /login.

    Les Réglages sont réservés à l'administrateur, pas aux opérateurs.
    """
    if is_private(request):
        return None
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


def _safe_next(value: str | None) -> str:
    """Redirection restreinte aux chemins internes de l'espace activation."""
    return security.safe_next(value, "/activation", prefix="/activation")


def _current_op(request: Request) -> str:
    return (request.cookies.get(COOKIE_OP) or "").upper()


def _ctx(request: Request, **extra: object) -> dict:
    ctx = {
        "callsign": activation.callsign(),
        "label": activation.label(),
        "current_op": _current_op(request),
        "operators": activation.list_operators(),
        "bands": activation.BANDS,
        "modes": activation.MODES,
        "is_admin": is_private(request),
        "station": activation.current_station(),
    }
    mode = _tz_mode(request)
    ctx["tz_mode"] = mode
    ctx["tz_label"] = activation.tz_label(mode)
    ctx["to_disp"] = lambda iso, m=mode: activation.disp(iso, m)
    ctx["cdt"] = lambda q, m=mode: activation.contact_disp(q.get("qso_date", ""), q.get("time_on", ""), m)
    ctx.update(extra)
    return ctx


# ── Auth opérateurs du club ──────────────────────────────────────────────────


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
            "configured": bool(activation.operator_password()),
            "last_call": "",
        },
    )


@router.post("/login")
async def operator_login_submit(
    request: Request,
    callsign: str = Form(""),
    password: str = Form(""),
    next: str = Form("/activation"),
) -> Response:
    target = _safe_next(next)
    expected = activation.operator_password()
    op = (callsign or "").strip().upper()
    blocked = security.login_blocked(visits.client_ip(request))
    error = None
    if blocked:
        error = "Trop de tentatives échouées : réessaie dans 15 minutes."
    elif not activation.valid_callsign(op):
        error = "Indicatif invalide"
    elif not expected:
        error = "Accès opérateur non configuré — voir l'admin du site"
    elif not password or password != expected:
        error = "Mot de passe incorrect"

    if not blocked:
        visits.record_auth(request, "operator", op, error is None)
    if error is None:
        # L'indicatif rejoint le roster et devient l'opérateur courant.
        try:
            activation.add_operator(op)
        except ValueError:
            pass
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(
            activation.OP_COOKIE, activation.make_op_token(),
            httponly=True, samesite="lax", max_age=activation.OP_TOKEN_TTL, path="/activation",
            secure=security.is_https(request),
        )
        resp.set_cookie(
            COOKIE_OP, op, max_age=30 * 86400, samesite="lax", path="/activation",
            secure=security.is_https(request),
        )
        return resp

    if not blocked:
        await asyncio.sleep(security.FAILED_LOGIN_DELAY)  # ralentit les essais de mots de passe
    return templates.TemplateResponse(
        request,
        "activation/login.html",
        {
            "callsign": activation.callsign(),
            "label": activation.label(),
            "station": activation.current_station(),
            "next": target,
            "error": error,
            "configured": bool(expected),
            "last_call": op,
        },
        status_code=429 if blocked else 401,
    )


@router.post("/logout")
async def operator_logout() -> Response:
    resp = RedirectResponse("/activation/login", status_code=303)
    resp.delete_cookie(activation.OP_COOKIE, path="/activation")
    return resp


@router.get("/tz")
async def set_timezone(request: Request, mode: str = "local", next: str = "/activation") -> Response:
    """Préférence d'affichage Local (Paris) / UTC. Le stockage reste UTC.

    Non protégé : c'est une simple préférence d'affichage (vaut aussi pour le
    board public). Cookie path=/ pour couvrir /activation et les pages /<indicatif>.
    """
    m = mode if mode in activation.TZ_MODES else "local"
    # Chemin interne uniquement (« //hote », « /\hote »… = redirection externe).
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
            operator_count=len(activation.active_operators()),
            planned_count=len(live) + len(upcoming),
            recent=activation.list_contacts(limit=10),
        ),
    )


# ── Opérateur courant ──────────────────────────────────────────────────────


@router.post("/whoami")
async def set_operator(request: Request, operator: str = Form(""), next: str = Form("/activation/log")) -> Response:
    if (g := _guard(request)) is not None:
        return g
    target = next if next.startswith("/activation") else "/activation/log"
    resp = RedirectResponse(target, status_code=303)
    cs = (operator or "").strip().upper()
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


# ── Réglages ───────────────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    if (g := _require_admin(request)) is not None:
        return g
    return _settings_page(request)


def _settings_page(request: Request, status_code: int = 200, **extra: object) -> Response:
    """Page Réglages (admin) ; ``extra`` : erreur / valeurs du formulaire d'indicatif."""
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
            callbook=activation.callbook_progress(),
            scoring=activation.get_scoring(),
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
    if (g := _require_admin(request)) is not None:
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
) -> Response:
    if (g := _require_admin(request)) is not None:
        return g
    activation.set_flag("show_contacts", bool(show_contacts))
    activation.set_flag("show_map_stats", bool(show_map_stats))
    return RedirectResponse("/activation/settings?fl=ok", status_code=303)


@router.post("/settings/scoring")
async def change_scoring(request: Request) -> Response:
    """Règle de points du classement (admin) : champs dynamiques par mode."""
    if (g := _require_admin(request)) is not None:
        return g
    form = await request.form()
    activation.set_scoring(dict(form))
    return RedirectResponse("/activation/settings?sc=ok#points", status_code=303)


# ── Indicatifs spéciaux (admin) ────────────────────────────────────────────

_STATION_FORM_FIELDS = ("label", "gridsquare", "start_date", "end_date", "badge", "subtitle", "flags", "public")


def _station_fields(form: object) -> dict:
    """Champs d'une fiche depuis le formulaire (case « public » absente = décochée)."""
    return {k: str(form.get(k, "")) for k in _STATION_FORM_FIELDS}  # type: ignore[attr-defined]


def _station_or_404(slug: str) -> dict:
    st = activation.station_by_slug(slug)
    if st is None:
        raise HTTPException(status_code=404)
    return st


@router.post("/stations")
async def station_create(request: Request) -> Response:
    if (g := _require_admin(request)) is not None:
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
    if (g := _require_admin(request)) is not None:
        return g
    st = _station_or_404(slug)
    return templates.TemplateResponse(
        request, "activation/edit_station.html", _ctx(request, st=st, form=st, error=None)
    )


@router.post("/stations/{slug}")
async def station_edit_submit(request: Request, slug: str) -> Response:
    if (g := _require_admin(request)) is not None:
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
    """Un seul indicatif en cours : l'espace opérateurs bascule sur celui-ci."""
    if (g := _require_admin(request)) is not None:
        return g
    activation.set_current_station(_station_or_404(slug)["callsign"])
    return RedirectResponse("/activation/settings?st=current#stations", status_code=303)


@router.post("/stations/{slug}/delete")
async def station_delete(request: Request, slug: str) -> Response:
    """Suppression d'une fiche sans QSO (refus expliqué sinon)."""
    if (g := _require_admin(request)) is not None:
        return g
    try:
        activation.delete_station(_station_or_404(slug)["callsign"])
    except ValueError as exc:
        return _settings_page(request, status_code=400, st_error=str(exc))
    return RedirectResponse("/activation/settings?st=deleted#stations", status_code=303)


@router.post("/settings/backup")
async def backup_now_route(request: Request) -> Response:
    if (g := _require_admin(request)) is not None:
        return g
    try:
        activation.backup_now()
        flash = "ok"
    except Exception:  # noqa: BLE001
        flash = "err"
    return RedirectResponse(f"/activation/settings?bk={flash}", status_code=303)


@router.get("/settings/backup.sqlite")
async def download_backup(request: Request) -> Response:
    if (g := _require_admin(request)) is not None:
        return g
    path = activation.backup_now()
    stamp = path.stem.replace("activation-", "")
    return FileResponse(
        path,
        media_type="application/x-sqlite3",
        filename=f"{activation.callsign().lower()}-activation-{stamp}.sqlite",
    )


# ── Planning des créneaux ──────────────────────────────────────────────────


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


def _adif_page(request: Request, preview: dict | None = None) -> Response:
    """Page ADIF : import (avec aperçu éventuel) + export d'une sélection."""
    contacts = activation.list_contacts()
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
    """Étape 1 : analyse du fichier et aperçu des QSO avec leur statut
    (doublons…). Rien n'est écrit dans le log à ce stade."""
    if (g := _guard(request)) is not None:
        return g
    text = (await file.read()).decode("utf-8", errors="replace")
    prefer_file = op_source == "file"
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
    """Étape 2 : importe les QSO cochés dans l'aperçu (jeton à usage unique)."""
    if (g := _guard(request)) is not None:
        return g
    text = activation.load_import(token)
    if text is None:
        return RedirectResponse("/activation/adif?err=expired", status_code=303)
    activation.drop_import(token)  # usage unique : pas de double import
    try:
        activation.backup_now()    # filet de sécurité avant un import en masse
    except Exception:  # noqa: BLE001
        pass
    rows = activation.analyze_adif(text, operator, op_source == "file")
    res = activation.import_rows(rows, set(sel))
    return RedirectResponse(
        f"/activation/adif?imported={res['added']}&skipped={res['skipped']}&invalid={res['invalid']}",
        status_code=303,
    )


@router.post("/export-selection.adi")
async def export_selection(request: Request, ids: list[int] = Form(default=[])) -> Response:
    """ADIF des seuls QSO cochés sur la page ADIF."""
    if (g := _guard(request)) is not None:
        return g
    contacts = activation.contacts_by_ids(ids)
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


@router.get("/slots/{slot_id}/edit", response_class=HTMLResponse)
async def edit_slot_form(request: Request, slot_id: int) -> Response:
    if (g := _guard(request)) is not None:
        return g
    slot = activation.get_slot(slot_id)
    if slot is None:
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
    tzm = _tz_mode(request)
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
            contacts=activation.list_contacts(limit=100),
            stats=activation.stats(),
            now_input=activation.now_input(_tz_mode(request)),
        ),
    )


@router.get("/qrz")
async def qrz_lookup_route(request: Request, call: str = "") -> Response:
    """Lookup QRZ pour la saisie du log : prénom, nom, locator, pays DXCC.

    Réservé à l'espace opérateurs (compte QRZ abonné du site, quota à ne pas
    ouvrir au public). Le résultat est mémorisé dans le callbook.
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
    return JSONResponse(
        {
            "call": cs, "found": True,
            "fname": row["fname"], "name": row["name"], "grid": row["grid"],
            "country": row["dxcc_name"] or row["country"],
        },
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
    op = (operator or _current_op(request)).strip().upper()
    dupe = activation.is_dupe(call, band, mode) if activation.valid_callsign(call) else False
    error = None
    try:
        freq_mhz = float(freq.replace(",", ".")) if freq.strip() else None
    except ValueError:
        freq_mhz = None
    # « Maintenant » coché (ou champ vide) → heure de validation, en UTC ;
    # sinon date/heure saisies dans le fuseau d'affichage courant.
    qso_date = time_on = None
    if when.strip() and not now:
        iso = activation.input_to_utc_iso(when, _tz_mode(request))
        parts = activation.utc_iso_to_parts(iso) if iso else None
        if parts:
            qso_date, time_on = parts
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
            "dupe": dupe,
            "error": error,
            "last_call": (call or "").strip().upper(),
        },
    )


@router.post("/contacts/{contact_id}/delete", response_class=HTMLResponse)
async def remove_contact(request: Request, contact_id: int) -> Response:
    if (g := _guard(request)) is not None:
        return g
    activation.delete_contact(contact_id)
    return templates.TemplateResponse(
        request,
        "activation/partials/log_result.html",
        {
            "contacts": activation.list_contacts(limit=100),
            "stats": activation.stats(),
            "dupe": False,
            "error": None,
            "last_call": "",
        },
    )


@router.get("/contacts/{contact_id}/edit", response_class=HTMLResponse)
async def edit_contact_form(request: Request, contact_id: int) -> Response:
    if (g := _guard(request)) is not None:
        return g
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
    body = activation.to_adif()
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
    body = activation.to_csv()
    fname = f"{activation.callsign().lower()}.csv"
    return PlainTextResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Board public (lecture seule) ───────────────────────────────────────────


def _public_stats(station: str, search_call: str = "") -> dict:
    """Carte / DXCC / classement d'un indicatif, si le réglage l'autorise.
    ``search_rank`` : place de l'indicatif recherché dans le classement."""
    if not activation.show_map_stats():
        return {"show_map_stats": False}
    grid = activation.my_gridsquare(station)
    ranking = activation.hunters_ranking(None, station)
    rule = activation.get_scoring(station)
    return {
        "show_map_stats": True,
        "map_data": activation.map_data(station),
        "dxcc": activation.dxcc_table(station),
        "ranking": ranking[:50],
        "search_rank": next((h for h in ranking if h["call"] == search_call), None),
        "scoring": rule,
        "scoring_summary": activation.scoring_summary(rule),
        "home": {"call": station, "grid": grid, "pos": activation.locator_center(grid)},
    }


@public_router.get("/activations", response_class=HTMLResponse)
async def stations_page(request: Request) -> Response:
    """Liste publique des indicatifs spéciaux du club (pages publiées)."""
    return templates.TemplateResponse(
        request,
        "activation/stations.html",
        {
            "callsign": club_config().get("callsign") or activation.callsign(),
            "label": club_config().get("name") or "Indicatifs spéciaux",
            "stations": [
                {**s, "status": activation.station_status(s)}
                for s in activation.list_stations() if s["public"]
            ],
        },
    )


_RE_SLUG = re.compile(r"^[a-z0-9]{3,16}$")


class _CallSlugConvertor(Convertor):
    """Segment d'indicatif : lettres/chiffres avec au moins un chiffre. La route
    générique ne capte ainsi ni /.git, /.ssh… ni /content (plus de 307)."""

    regex = "[A-Za-z0-9]*[0-9][A-Za-z0-9]*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("callslug", _CallSlugConvertor())


@public_router.get("/{slug:callslug}", response_class=HTMLResponse)
async def public_board(request: Request, slug: str, call: str = "") -> Response:
    """Page publique d'un indicatif spécial : /tm25test, /tm61xyz…

    Route générique à un segment, incluse en dernier (main.py) : tout ce qui
    n'est pas un indicatif publié répond 404, comme avant.
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
            "slug": st["slug"],
            "callsign": cs,
            "label": st["label"] or cs,
            "status": activation.station_status(st),
            "stats": activation.stats(cs),
            "slots": activation.future_slots(cs),
            "live": activation.live_slots(cs),
            "past": activation.past_slots(cs),
            "recent": activation.list_contacts(limit=50, station=cs),
            "search": search,
            "search_call": call.strip().upper(),
            "show_contacts": activation.show_contacts(),
            **_public_stats(cs, call.strip().upper()),
            "tz_mode": mode,
            "tz_label": activation.tz_label(mode),
            "to_disp": lambda iso, m=mode: activation.disp(iso, m),
            "cdt": lambda q, m=mode: activation.contact_disp(q.get("qso_date", ""), q.get("time_on", ""), m),
        },
    )
