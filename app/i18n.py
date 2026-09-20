"""Traduction du module d'activation (français d'origine, anglais…).

Les textes restent écrits en français dans le code et les templates, entourés
de ``_()`` ; ``locales/<langue>.json`` donne leur traduction (clé = texte
français exact). Un texte absent du catalogue s'affiche en français : jamais
de page cassée, et tests/test_i18n.py signale les oublis.

Langue d'une requête : cookie ``lang`` (bouton FR | EN), sinon la langue du
navigateur (Accept-Language : français → fr, toute autre langue → en), sinon
français. Elle est fixée par la dépendance ``request_lang`` des routeurs, dans
une ContextVar lue par ``_()`` (templates, messages d'erreur, fils de travail).

Ajouter une langue : locales/<code>.json + une entrée dans LANGS.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request

LANGS = {"fr": "Français", "en": "English"}
DEFAULT = "fr"
COOKIE = "lang"
COOKIE_MAX_AGE = 365 * 86400
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_lang: ContextVar[str] = ContextVar("activation_lang", default=DEFAULT)


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    """Traductions d'une langue (texte français → texte traduit)."""
    if lang == DEFAULT:
        return {}
    try:
        data = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def from_accept_language(header: str | None) -> str:
    """Langue proposée d'après Accept-Language : la première langue gérée dans
    l'ordre de préférence ; aucune gérée (allemand, italien…) → anglais ;
    en-tête absent (robots, scripts) → français."""
    prefs = []
    for i, part in enumerate((header or "").split(",")):
        tag, *params = [p.strip() for p in part.split(";")]
        q = 1.0
        for p in params:
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        if tag and q > 0:
            prefs.append((-q, i, tag.lower().split("-")[0]))
    if not prefs:   # en-tête présent mais tout refusé (q=0) : un navigateur, pas un robot
        return "en" if (header or "").strip() else DEFAULT
    for _q, _i, base in sorted(prefs):
        if base in LANGS:
            return base
    return "en"


def detect(request: Request) -> str:
    chosen = request.cookies.get(COOKIE, "")
    if chosen in LANGS:
        return chosen
    return from_accept_language(request.headers.get("accept-language"))


def use(lang: str) -> None:
    _lang.set(lang if lang in LANGS else DEFAULT)


def current() -> str:
    return _lang.get()


async def request_lang(request: Request) -> str:
    """Dépendance FastAPI des routeurs : fixe la langue de la requête.

    Asynchrone exprès : exécutée dans la tâche de la route, la ContextVar
    reste visible dans la route, ses templates et run_in_threadpool."""
    lang = detect(request)
    use(lang)
    return lang


def gettext(message: str, **params: Any) -> str:
    """Traduit ``message`` (texte français) ; ``params`` : champs {nom} à remplir."""
    lang = _lang.get()
    # Clé sur une ligne : un texte coupé sur plusieurs lignes dans un template
    # garde la même traduction.
    text = message if lang == DEFAULT else catalog(lang).get(" ".join(message.split()), message)
    return text.format(**params) if params else text


_ = gettext


def N_(message: str) -> str:
    """Marque un texte à traduire à l'affichage (libellés de tables) : inchangé ici."""
    return message


# ── Dates ───────────────────────────────────────────────────────────────────

_DOW = {
    "fr": ("lun", "mar", "mer", "jeu", "ven", "sam", "dim"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def dow(value: Any) -> str:
    """Jour de la semaine abrégé (« sam » / « Sat ») ; '' si invalide."""
    d = _as_date(value)
    return _DOW.get(current(), _DOW[DEFAULT])[d.weekday()] if d else ""


def day_month(value: Any) -> str:
    """Jour et mois : « 07/09 » en français, « 7 Sep » en anglais (sans ambiguïté jour/mois)."""
    d = _as_date(value)
    if not d:
        return ""
    return f"{d.day} {_MONTHS_EN[d.month - 1]}" if current() == "en" else d.strftime("%d/%m")


def date_long(value: Any) -> str:
    """Date complète : « 07/09/2026 » en français, « 7 Sep 2026 » en anglais."""
    d = _as_date(value)
    if not d:
        return ""
    return f"{d.day} {_MONTHS_EN[d.month - 1]} {d.year}" if current() == "en" else d.strftime("%d/%m/%Y")


def date_short(value: Any) -> str:
    """Date de tableau : « 07/09/26 » en français, « 2026-09-07 » en anglais."""
    d = _as_date(value)
    if not d:
        return ""
    return d.isoformat() if current() == "en" else d.strftime("%d/%m/%y")


def ordinal(n: int) -> str:
    """Suffixe de rang : 1er, 2e… / 1st, 2nd, 3rd, 4th…"""
    if current() == "en":
        if n % 100 in (11, 12, 13):
            return "th"
        return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "er" if n == 1 else "e"


# ── Templates et JavaScript ─────────────────────────────────────────────────

# Textes affichés par les scripts (static/js/activation-*.js, via t("…")) :
# seuls ceux-ci sont envoyés à la page, dans window.ACT_I18N.
JS_MESSAGES: tuple[str, ...] = (
    "{d}j",
    "⏳ Débute dans {t}",
    "🔴 En direct — fin dans {t}",
    "✓ Terminé",
    "{n} / {total} sélectionné",
    "{n} / {total} sélectionnés",
)


def js_catalog() -> dict[str, str]:
    """Traductions des textes JavaScript pour la langue courante ({} en français)."""
    if current() == DEFAULT:
        return {}
    return {m: gettext(m) for m in JS_MESSAGES}


def install(env: Any) -> None:
    """Rend _(), la langue et les dates localisées disponibles dans les templates."""
    # Textes traduits passés au JavaScript (|tojson) : accents gardés tels quels,
    # la page est en UTF-8 (sinon « contactée » devient « contact\u00e9e »).
    env.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}
    env.globals.update(
        _=gettext,
        current_lang=current,
        lang_names=LANGS,
        js_catalog=js_catalog,
        dow=dow,
        day_month=day_month,
        date_long=date_long,
        date_short=date_short,
        ordinal=ordinal,
    )
