"""Translation of the activation module (French source, English…).

Texts stay written in French in the code and templates, wrapped in ``_()``;
``locales/<lang>.json`` provides their translation (key = exact French text).
A text missing from the catalog is shown in French: never a broken page, and
tests/test_i18n.py reports the missing ones.

Language of a request: the ``lang`` cookie (FR | EN button), otherwise the
browser language (Accept-Language: French → fr, any other language → en),
otherwise French. It is set by the routers' ``request_lang`` dependency, in a
ContextVar read by ``_()`` (templates, error messages, worker threads).

Adding a language: locales/<code>.json + an entry in LANGS.
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
    """Translations for one language (French text → translated text)."""
    if lang == DEFAULT:
        return {}
    try:
        data = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def from_accept_language(header: str | None) -> str:
    """Language suggested by Accept-Language: the first supported language in
    order of preference; none supported (German, Italian…) → English;
    header missing (bots, scripts) → French."""
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
    if not prefs:   # header present but everything refused (q=0): a browser, not a bot
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
    """FastAPI dependency for the routers: sets the request language.

    Async on purpose: it runs in the route's task, so the ContextVar stays
    visible in the route, its templates and run_in_threadpool."""
    lang = detect(request)
    use(lang)
    return lang


def gettext(message: str, **params: Any) -> str:
    """Translate ``message`` (French text); ``params``: {name} fields to fill in."""
    lang = _lang.get()
    # Key on a single line: a text split over several lines in a template
    # keeps the same translation.
    text = message if lang == DEFAULT else catalog(lang).get(" ".join(message.split()), message)
    return text.format(**params) if params else text


_ = gettext


def N_(message: str) -> str:
    """Mark a text to be translated at display time (table labels): unchanged here."""
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
    """Abbreviated day of the week ("sam" / "Sat"); '' if invalid."""
    d = _as_date(value)
    return _DOW.get(current(), _DOW[DEFAULT])[d.weekday()] if d else ""


def day_month(value: Any) -> str:
    """Day and month: "07/09" in French, "7 Sep" in English (no day/month ambiguity)."""
    d = _as_date(value)
    if not d:
        return ""
    return f"{d.day} {_MONTHS_EN[d.month - 1]}" if current() == "en" else d.strftime("%d/%m")


def date_long(value: Any) -> str:
    """Full date: "07/09/2026" in French, "7 Sep 2026" in English."""
    d = _as_date(value)
    if not d:
        return ""
    return f"{d.day} {_MONTHS_EN[d.month - 1]} {d.year}" if current() == "en" else d.strftime("%d/%m/%Y")


def date_short(value: Any) -> str:
    """Table date: "07/09/26" in French, "2026-09-07" in English."""
    d = _as_date(value)
    if not d:
        return ""
    return d.isoformat() if current() == "en" else d.strftime("%d/%m/%y")


def ordinal(n: int) -> str:
    """Ordinal suffix: 1er, 2e… / 1st, 2nd, 3rd, 4th…"""
    if current() == "en":
        if n % 100 in (11, 12, 13):
            return "th"
        return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "er" if n == 1 else "e"


# ── Templates and JavaScript ────────────────────────────────────────────────

# Texts shown by the scripts (static/js/activation-*.js, via t("…")):
# only these are sent to the page, in window.ACT_I18N.
JS_MESSAGES: tuple[str, ...] = (
    "{d}j",
    "⏳ Débute dans {t}",
    "🔴 En direct — fin dans {t}",
    "✓ Terminé",
    "{n} / {total} sélectionné",
    "{n} / {total} sélectionnés",
)


def js_catalog() -> dict[str, str]:
    """Translations of the JavaScript texts for the current language ({} in French)."""
    if current() == DEFAULT:
        return {}
    return {m: gettext(m) for m in JS_MESSAGES}


def install(env: Any) -> None:
    """Make _(), the language and localized dates available in templates."""
    # Translated texts passed to JavaScript (|tojson): accents kept as is, the
    # page is UTF-8 (otherwise "contactée" becomes "contact\u00e9e").
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
