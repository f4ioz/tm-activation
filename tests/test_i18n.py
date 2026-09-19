"""Traduction du module d'activation : catalogue complet, détection de langue.

Le rendu des pages en anglais est testé dans test_activation.py (base isolée).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import i18n

ROOT = Path(__file__).resolve().parent.parent
_MSG = re.compile(r'\b(?:_|N_)\(\s*"((?:[^"\\]|\\.)*)"')
_JS_MSG = re.compile(r"\bt\(\s*(?:n > 1 \? )?'([^']*)'(?:\s*:\s*'([^']*)')?")


def _norm(text: str) -> str:
    return " ".join(text.replace('\\"', '"').split())


def message_ids() -> dict[str, str]:
    """Textes marqués _("…") / N_("…") : templates d'activation, modules Python
    qui utilisent app.i18n, textes JavaScript."""
    files = [*ROOT.glob("templates/activation/**/*.html"), *ROOT.glob("templates/login.html")]
    files += [f for f in ROOT.glob("app/**/*.py") if "from app.i18n import" in f.read_text(encoding="utf-8")]
    ids: dict[str, str] = {}
    for f in files:
        for m in _MSG.finditer(f.read_text(encoding="utf-8")):
            ids.setdefault(_norm(m.group(1)), str(f.relative_to(ROOT)))
    for m in i18n.JS_MESSAGES:
        ids.setdefault(m, "app/i18n.py (JS_MESSAGES)")
    return ids


@pytest.mark.parametrize("lang", [code for code in i18n.LANGS if code != i18n.DEFAULT])
def test_catalog_translates_every_message(lang: str) -> None:
    data = json.loads((i18n.LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    missing = sorted(f"{src}: {m}" for m, src in message_ids().items() if not data.get(m))
    assert not missing, f"{len(missing)} texte(s) sans traduction {lang} :\n" + "\n".join(missing)
    fields = re.compile(r"\{(\w+)\}")
    for fr, tr in data.items():
        assert set(fields.findall(fr)) == set(fields.findall(tr)), f"champs {{…}} différents : {fr!r} → {tr!r}"


def test_js_messages_are_declared() -> None:
    """Chaque t('…') des scripts doit figurer dans JS_MESSAGES (envoyé à la page)."""
    used = set()
    for f in ROOT.glob("static/js/activation-*.js"):
        for m in _JS_MSG.finditer(f.read_text(encoding="utf-8")):
            used.update(x for x in m.groups() if x)
    assert used and used <= set(i18n.JS_MESSAGES), used - set(i18n.JS_MESSAGES)


@pytest.mark.parametrize("header, lang", [
    (None, "fr"),                                  # robots, scripts : français
    ("", "fr"),
    ("fr-FR,fr;q=0.9,en;q=0.8", "fr"),
    ("en-US,en;q=0.9", "en"),
    ("de-DE,de;q=0.9", "en"),                      # langue non gérée → anglais
    ("de-DE,de;q=0.9,fr;q=0.8", "fr"),             # le français est dans la liste
    ("it;q=1.0,en;q=0.5,fr;q=0.8", "fr"),          # ordre des q, pas de l'en-tête
    ("fr;q=0", "en"),                              # q=0 : refusé
    ("es;q=abc", "en"),
])
def test_accept_language(header: str | None, lang: str) -> None:
    assert i18n.from_accept_language(header) == lang


def test_gettext_and_dates() -> None:
    i18n.use("en")
    try:
        assert i18n.gettext("Réglages") == "Settings"
        assert i18n.gettext("Du {start} au {end}", start="1", end="2") == "From 1 to 2"
        assert i18n.gettext("texte inconnu du catalogue") == "texte inconnu du catalogue"
        assert i18n.date_long("2026-09-07") == "7 Sep 2026" and i18n.day_month("2026-09-07") == "7 Sep"
        assert i18n.dow("2026-09-07") == "Mon" and [i18n.ordinal(n) for n in (1, 2, 3, 4, 11, 22)] == [
            "st", "nd", "rd", "th", "th", "nd"]
        assert i18n.js_catalog()["✓ Terminé"] == "✓ Finished"
    finally:
        i18n.use("fr")
    assert i18n.gettext("Réglages") == "Réglages" and i18n.date_long("2026-09-07") == "07/09/2026"
    assert i18n.dow("2026-09-07") == "lun" and i18n.js_catalog() == {}
    i18n.use("xx")                                 # langue inconnue → français
    assert i18n.current() == "fr"
