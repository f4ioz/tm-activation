"""Activation d'un indicatif temporaire de club (ex. TM25TEST).

Couche données SQLite brute (même style que ``app/db.py``) mais dans une base
**séparée** (``var/activation.sqlite``) : la base ``f4ioz.sqlite`` est vidée à
chaque refresh Wavelog, on ne veut surtout pas y mêler le log d'activation.

Tables (créneaux et QSO portent l'indicatif spécial : colonne ``station``) :
- ``stations``  : indicatifs spéciaux du club, un seul « en cours » à la fois ;
- ``operators`` : liste des opérateurs autorisés à émettre sous l'indicatif ;
- ``slots``     : créneaux réservés (qui / quand / bande / mode) ;
- ``contacts``  : QSO loggés (indicatifs contactés) ;
- ``callbook``  : cache des fiches QRZ des indicatifs contactés (nom, locator,
  DXCC), rempli à la saisie et par une tâche de fond douce.

Heures stockées en UTC. Les créneaux en ISO ``YYYY-MM-DDTHH:MM`` ; les contacts
au format ADIF (``qso_date`` ``YYYYMMDD`` + ``time_on`` ``HHMM``).
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import math
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import auth as _auth, dxcc_flags
from app.config import activation_config, qrz_config, site_config
from app.i18n import N_, _
from app.qrz_xml import QrzXmlClient, get_shared_client

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "var" / "activation.sqlite"
BACKUP_DIR = ROOT / "var" / "backups"
BACKUP_KEEP = 40                 # nb de snapshots conservés (rotation)
_BACKUP_MIN_INTERVAL = 600       # throttle : au plus 1 sauvegarde / 10 min sur écriture
_last_backup_ts = 0.0

PARIS = ZoneInfo("Europe/Paris")
UTC = timezone.utc

# Bandes / modes proposés dans les formulaires (HF + VHF/UHF usuelles).
BANDS = [
    "160M", "80M", "60M", "40M", "30M", "20M", "17M", "15M", "12M", "10M",
    "6M", "4M", "2M", "70CM", "23CM",
]
MODES = ["SSB", "CW", "FT8", "FT4", "RTTY", "PSK31", "FM", "AM", "SSTV", "DIGI"]

_RE_CALLSIGN = re.compile(r"^[A-Z0-9]{3,10}(/[A-Z0-9]{1,4})?$")
_RE_LOCATOR = re.compile(r"^[A-R]{2}\d{2}([A-X]{2})?$")
_RE_LOCATOR8 = re.compile(r"^[A-R]{2}\d{2}[A-X]{2}\d{2}$")  # station (ex. JN18FS89)


# ── Indicatifs spéciaux (stations) ─────────────────────────────────────────
# Le club active des indicatifs spéciaux de temps à autre (TM25TEST, puis
# d'autres). Un seul est « en cours » à la fois (réglage admin) : c'est lui que
# l'espace opérateurs logue et planifie ; les autres restent consultables sur
# leur page publique /<slug>. Liste des opérateurs, mot de passe et callbook QRZ
# sont communs à tous les indicatifs.

# Drapeaux dessinés en CSS (activation.css) → préfixe radio affiché à côté.
FLAG_PREFIXES = {"fr": "F", "be": "ON", "de": "DL", "it": "I", "nl": "PA", "lu": "LX", "es": "EA"}
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_SAT_NOTE = re.compile(r"\bsat(?:ellites?|s)?\b", re.I)


def note_is_sat(note: str | None) -> bool:
    """Note de créneau qui parle de satellite (« SAT FO-29 », « QRV Sat SO-50 »…)
    → icône 🛰️ dans les vignettes. Mot entier : « Samedi », « Saturne » exclus."""
    return bool(_RE_SAT_NOTE.search(note or ""))


def slugify_call(call: str) -> str:
    """Indicatif → segment d'URL publique : TM25TEST → tm25test."""
    return re.sub(r"[^a-z0-9]", "", (call or "").lower())


def list_stations() -> list[dict[str, Any]]:
    """Tous les indicatifs spéciaux (plus récents d'abord), avec leur nombre de QSO."""
    init_db()
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM contacts ct WHERE ct.station = s.callsign) AS qsos, "
            "(SELECT COUNT(*) FROM slots sl WHERE sl.station = s.callsign) AS slots "
            "FROM stations s ORDER BY s.start_date DESC, s.id DESC"
        ).fetchall()]


def get_station(call: str) -> dict[str, Any] | None:
    init_db()
    with conn() as c:
        row = c.execute(
            "SELECT * FROM stations WHERE callsign=?", ((call or "").strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def station_by_slug(slug: str) -> dict[str, Any] | None:
    init_db()
    with conn() as c:
        row = c.execute("SELECT * FROM stations WHERE slug=?", ((slug or "").strip().lower(),)).fetchone()
    return dict(row) if row else None


def current_station() -> dict[str, Any]:
    """Indicatif spécial en cours (réglage admin), à défaut le premier créé."""
    st = get_station(load_settings().get("current_station") or "")
    if st is None:
        with conn() as c:  # init_db() déjà fait par get_station
            st = dict(c.execute("SELECT * FROM stations ORDER BY id LIMIT 1").fetchone())
    return st


def _st(station: str | None = None) -> str:
    """Indicatif visé : celui passé en paramètre, sinon celui en cours."""
    return (station or current_station()["callsign"]).strip().upper()


def callsign() -> str:
    """Indicatif spécial en cours."""
    return current_station()["callsign"]


def label() -> str:
    st = current_station()
    return st["label"] or st["callsign"]


def my_gridsquare(station: str | None = None) -> str:
    """Locator d'un indicatif spécial (défaut : en cours) — ADIF et distances."""
    st = get_station(station) if station else current_station()
    return ((st or {}).get("gridsquare") or "").upper()


def is_public() -> bool:
    """La page publique de l'indicatif en cours est-elle en ligne ?"""
    return bool(current_station()["public"])


def station_status(st: dict[str, Any]) -> str:
    """« current » (en cours), « upcoming » (début daté dans le futur) ou « archive »."""
    if st["callsign"] == callsign():
        return "current"
    if st.get("start_date") and st["start_date"] > datetime.now(PARIS).strftime("%Y-%m-%d"):
        return "upcoming"
    return "archive"


def set_current_station(call: str) -> None:
    """Bascule l'espace opérateurs (log, planning, ADIF, points) sur ``call``."""
    st = get_station(call)
    if st is None:
        raise ValueError(_("indicatif inconnu"))
    data = load_settings()
    data["current_station"] = st["callsign"]
    _save_settings(data)


def _clean_station(fields: dict[str, Any]) -> dict[str, Any]:
    """Champs modifiables d'une fiche, validés (ValueError sinon)."""
    grid = str(fields.get("gridsquare") or "").strip().upper()
    if grid and not (_RE_LOCATOR.match(grid) or _RE_LOCATOR8.match(grid)):
        raise ValueError(_("locator invalide (4, 6 ou 8 caractères)"))
    dates = {}
    for key in ("start_date", "end_date"):
        value = str(fields.get(key) or "").strip()
        if value and not _RE_DATE.match(value):
            raise ValueError(_("date invalide (AAAA-MM-JJ)"))
        dates[key] = value
    if dates["start_date"] and dates["end_date"] and dates["end_date"] < dates["start_date"]:
        raise ValueError(_("date de fin avant la date de début"))
    flags = [f.strip().lower() for f in str(fields.get("flags") or "").split(",")]
    return {
        "label": str(fields.get("label") or "").strip()[:120],
        "gridsquare": grid,
        **dates,
        "public": 1 if fields.get("public") else 0,
        "badge": str(fields.get("badge") or "").strip()[:30],
        "subtitle": str(fields.get("subtitle") or "").strip()[:160],
        "flags": ",".join(f for f in flags if f in FLAG_PREFIXES),
    }


def create_station(call: str, **fields: Any) -> dict[str, Any]:
    """Nouvel indicatif spécial (il ne devient pas « en cours » tout seul)."""
    cs = (call or "").strip().upper()
    slug = slugify_call(cs)
    # Un chiffre dans le slug : un indicatif ne peut pas masquer une page du site (/grid…).
    if not valid_callsign(cs) or not re.search(r"\d", slug):
        raise ValueError(_("indicatif invalide"))
    data = _clean_station(fields)
    init_db()
    with conn() as c:
        if c.execute("SELECT 1 FROM stations WHERE callsign=? OR slug=?", (cs, slug)).fetchone():
            raise ValueError(_("indicatif déjà enregistré"))
        c.execute(
            "INSERT INTO stations(callsign, slug, label, gridsquare, start_date, end_date, public, "
            "badge, subtitle, flags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cs, slug, data["label"], data["gridsquare"], data["start_date"], data["end_date"],
             data["public"], data["badge"], data["subtitle"], data["flags"], int(time.time())),
        )
    maybe_backup()
    return get_station(cs)


def update_station(call: str, **fields: Any) -> dict[str, Any]:
    """Modifie une fiche. L'indicatif lui-même ne change pas : il signe les QSO."""
    st = get_station(call)
    if st is None:
        raise ValueError(_("indicatif inconnu"))
    data = _clean_station({**st, **fields})
    with conn() as c:
        c.execute(
            "UPDATE stations SET label=?, gridsquare=?, start_date=?, end_date=?, public=?, "
            "badge=?, subtitle=?, flags=? WHERE callsign=?",
            (data["label"], data["gridsquare"], data["start_date"], data["end_date"],
             data["public"], data["badge"], data["subtitle"], data["flags"], st["callsign"]),
        )
    maybe_backup()
    return get_station(st["callsign"])


def delete_station(call: str) -> None:
    """Supprime une fiche SANS QSO (ses créneaux planifiés partent avec elle).

    Refusé pour l'indicatif en cours et dès qu'un QSO porte cet indicatif : un
    log ne se perd jamais. Sauvegarde complète juste avant la suppression.
    """
    st = get_station(call)
    if st is None:
        raise ValueError(_("indicatif inconnu"))
    cs = st["callsign"]
    if cs == callsign():
        raise ValueError(_("{call} est l'indicatif en cours : mets-en un autre en cours d'abord", call=cs))
    backup_now()
    with conn() as c:
        # Vérifié dans la même transaction que la suppression.
        if c.execute("SELECT 1 FROM contacts WHERE station=? LIMIT 1", (cs,)).fetchone():
            raise ValueError(_("{call} a des QSO : sa fiche est conservée", call=cs))
        c.execute("DELETE FROM slots WHERE station=?", (cs,))
        c.execute("DELETE FROM stations WHERE callsign=?", (cs,))
    data = load_settings()
    if cs in (data.get("scoring_by_station") or {}):
        del data["scoring_by_station"][cs]
        _save_settings(data)


# ── Réglages persistants (flags togglés via l'UI admin) ────────────────────

SETTINGS_FILE = ROOT / "var" / "activation_settings.json"


def load_settings() -> dict[str, Any]:
    try:
        if SETTINGS_FILE.is_file():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        pass
    return {}


def get_flag(key: str, default: bool = False) -> bool:
    return bool(load_settings().get(key, default))


def _save_settings(data: dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")


def set_flag(key: str, value: bool) -> None:
    data = load_settings()
    data[key] = bool(value)
    _save_settings(data)


def show_contacts() -> bool:
    """Afficher la liste des contacts sur le board public ? (défaut : non)."""
    return get_flag("show_contacts", False)


def show_map_stats() -> bool:
    """Carte, tableau DXCC et classement sur le board public ? (défaut : oui)."""
    return get_flag("show_map_stats", True)


# ── Auth opérateurs du club ──────────────────────────────────────────────────
# Session dédiée à l'espace TM25TEST, DISTINCTE du mode privé admin du site.
# Le jeton signe "activation:{ts}" (séparation de domaine) : un cookie
# opérateur ne peut donc pas être réutilisé comme cookie admin f4ioz_priv.

OP_COOKIE = "tm_auth"
OP_TOKEN_TTL = 12 * 3600  # 12 h
OP_PASSWORD_FILE = ROOT / "var" / "activation_password"


def operator_password() -> str:
    """Mot de passe des opérateurs du club.

    Priorité au fichier ``var/activation_password`` (réglable via l'UI) ; à
    défaut ``activation.password`` de config.yml. Vide → auth opérateur non
    configurée (seul l'admin site peut entrer, pour l'amorçage).
    """
    try:
        if OP_PASSWORD_FILE.is_file():
            pw = OP_PASSWORD_FILE.read_text(encoding="utf-8").strip()
            if pw:
                return pw
    except OSError:
        pass
    return str(activation_config().get("password") or "")


def set_operator_password(new_password: str) -> None:
    pw = (new_password or "").strip()
    if not pw:
        raise ValueError(_("mot de passe vide"))
    OP_PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    OP_PASSWORD_FILE.write_text(pw, encoding="utf-8")
    OP_PASSWORD_FILE.chmod(0o600)
    # Force une sauvegarde immédiate (le mot de passe est critique).
    try:
        backup_now()
    except Exception:  # noqa: BLE001
        pass


def _op_sig(ts: str, call: str = "") -> str:
    payload = f"activation:{ts}:{call}" if call else f"activation:{ts}"
    return hmac.new(_auth.get_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_op_token(call: str = "", now: int | None = None) -> str:
    """Jeton de session opérateur. Avec ``call``, il ne vaut QUE pour cet
    indicatif : le compte d'un autre opérateur ne peut pas être emprunté."""
    ts = str(int(time.time()) if now is None else int(now))
    cs = (call or "").strip().upper()
    return f"{ts}.{cs}.{_op_sig(ts, cs)}" if cs else f"{ts}.{_op_sig(ts)}"


def op_session(token: str | None) -> dict[str, str] | None:
    """Contenu d'un jeton valide : {"call": indicatif} ("" pour le mot de passe commun)."""
    if not token or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) == 2:
        ts_str, sig, call = parts[0], parts[1], ""
    elif len(parts) == 3:
        ts_str, call, sig = parts
    else:
        return None
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    if abs(int(time.time()) - ts) > OP_TOKEN_TTL:
        return None
    if not hmac.compare_digest(_op_sig(ts_str, call), sig):
        return None
    return {"call": call}


def verify_op_token(token: str | None) -> bool:
    return op_session(token) is not None


def session_operator(token: str | None) -> str:
    """Indicatif du compte connecté ("" avec le mot de passe commun)."""
    sess = op_session(token)
    return sess["call"] if sess else ""


def operator_authed(token: str | None) -> bool:
    """Session opérateur valide : compte actif (mot de passe par opérateur) ou
    mot de passe commun configuré."""
    sess = op_session(token)
    if sess is None:
        return False
    if per_operator_auth():
        row = get_operator(sess["call"]) if sess["call"] else None
        return bool(row and row.get("active") and row.get("status", "active") == "active"
                    and row.get("password_hash"))
    return bool(operator_password())


# ── Comptes opérateurs (option : un mot de passe par opérateur) ────────────
# Par défaut, tous les opérateurs partagent un mot de passe (operator_password).
# Réglage « per_operator_auth » : chacun se connecte avec SON mot de passe, créé
# à sa première connexion. Réglage « operator_approval » : un compte nouveau
# attend l'accord d'un administrateur. Un opérateur déjà dans la liste (ajouté par
# un admin) n'a rien à faire valider : il choisit son mot de passe et entre.

PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    """Empreinte salée d'un mot de passe (jamais stocké en clair)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def per_operator_auth() -> bool:
    """Un mot de passe par opérateur (sinon : mot de passe commun)."""
    return get_flag("per_operator_auth", False)


def operator_approval() -> bool:
    """Les comptes créés à la volée attendent la validation d'un administrateur."""
    return get_flag("operator_approval", False)


# Exigences du mot de passe d'un compte opérateur (création et remise à zéro),
# réglables dans les Réglages : longueur minimale et nombre de majuscules, de
# chiffres et de caractères spéciaux exigés (0 = pas d'exigence).
PASSWORD_MIN_LEN = 8              # défaut historique
PASSWORD_LEN_MAX = 64             # borne haute du réglage de longueur
PASSWORD_COUNT_MAX = 8            # borne haute des compteurs (majuscules…)
DEFAULT_PASSWORD_RULE: dict[str, int] = {
    "min_length": PASSWORD_MIN_LEN, "min_upper": 1, "min_digits": 1, "min_special": 1,
}
_RE_UPPER = re.compile(r"[A-ZÀ-Þ]")
_RE_DIGIT = re.compile(r"[0-9]")
_RE_SPECIAL = re.compile(r"[^0-9A-Za-zÀ-ÿ]")


def get_password_rule() -> dict[str, int]:
    """Exigences en vigueur pour les mots de passe opérateurs."""
    saved = load_settings().get("password_rule") or {}
    d = DEFAULT_PASSWORD_RULE
    return {
        "min_length": _clamp_int(saved.get("min_length"), 4, PASSWORD_LEN_MAX, d["min_length"]),
        "min_upper": _clamp_int(saved.get("min_upper"), 0, PASSWORD_COUNT_MAX, d["min_upper"]),
        "min_digits": _clamp_int(saved.get("min_digits"), 0, PASSWORD_COUNT_MAX, d["min_digits"]),
        "min_special": _clamp_int(saved.get("min_special"), 0, PASSWORD_COUNT_MAX, d["min_special"]),
    }


def set_password_rule(form: dict[str, Any]) -> dict[str, int]:
    """Enregistre les exigences (valeurs hors bornes ramenées au défaut)."""
    d = DEFAULT_PASSWORD_RULE
    rule = {
        "min_length": _clamp_int(form.get("min_length"), 4, PASSWORD_LEN_MAX, d["min_length"]),
        "min_upper": _clamp_int(form.get("min_upper"), 0, PASSWORD_COUNT_MAX, d["min_upper"]),
        "min_digits": _clamp_int(form.get("min_digits"), 0, PASSWORD_COUNT_MAX, d["min_digits"]),
        "min_special": _clamp_int(form.get("min_special"), 0, PASSWORD_COUNT_MAX, d["min_special"]),
    }
    data = load_settings()
    data["password_rule"] = rule
    _save_settings(data)
    return rule


def password_rule() -> str:
    """Règle affichée sur la page de connexion, d'après le réglage en vigueur."""
    rule = get_password_rule()
    bits = []
    if rule["min_upper"]:
        bits.append(_("{n} majuscule", n=rule["min_upper"]) if rule["min_upper"] == 1
                    else _("{n} majuscules", n=rule["min_upper"]))
    if rule["min_digits"]:
        bits.append(_("{n} chiffre", n=rule["min_digits"]) if rule["min_digits"] == 1
                    else _("{n} chiffres", n=rule["min_digits"]))
    if rule["min_special"]:
        bits.append(_("{n} caractère spécial", n=rule["min_special"]) if rule["min_special"] == 1
                    else _("{n} caractères spéciaux", n=rule["min_special"]))
    if not bits:
        return _("Au moins {n} caractères.", n=rule["min_length"])
    return _("Au moins {n} caractères, dont {details}.",
             n=rule["min_length"], details=", ".join(bits))


def password_is_strong(password: str) -> bool:
    pw = password or ""
    rule = get_password_rule()
    return bool(len(pw) >= rule["min_length"]
                and len(_RE_UPPER.findall(pw)) >= rule["min_upper"]
                and len(_RE_DIGIT.findall(pw)) >= rule["min_digits"]
                and len(_RE_SPECIAL.findall(pw)) >= rule["min_special"])


# ── Question anti-robot (sans service extérieur, sans état serveur) ────────
# Le jeton signe l'heure ET la bonne réponse : on revérifie la signature avec la
# réponse envoyée, sans garder la question côté serveur. Un formulaire rempli en
# moins de MIN_FILL_SECONDS, ou dont le champ-piège est rempli, vient d'un robot.

CAPTCHA_TTL = 900          # 15 min pour répondre
CAPTCHA_MIN_FILL = 2.0     # un humain met plus de 2 s à remplir le formulaire
CAPTCHA_TRAP = "website"   # champ-piège, masqué : les robots le remplissent


def _captcha_sig(ts: str, answer: int) -> str:
    return hmac.new(_auth.get_secret(), f"captcha:{ts}:{answer}".encode(), hashlib.sha256).hexdigest()


def make_captcha() -> dict[str, str]:
    """Question arithmétique simple + jeton signé ({"question", "token"})."""
    a, b = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
    ts = str(int(time.time()))
    return {"question": f"{a} + {b}", "token": f"{ts}.{_captcha_sig(ts, a + b)}"}


def check_captcha(token: str | None, answer: str | None, trap: str | None = "") -> bool:
    """Réponse juste, jeton frais, formulaire ni instantané ni pré-rempli par un robot."""
    if (trap or "").strip():
        return False
    if not token or "." not in token:
        return False
    ts_str, sig = token.split(".", 1)
    try:
        ts, value = int(ts_str), int((answer or "").strip())
    except ValueError:
        return False
    age = time.time() - ts
    if age < CAPTCHA_MIN_FILL or age > CAPTCHA_TTL:
        return False
    return hmac.compare_digest(_captcha_sig(ts_str, value), sig)


def get_operator(call: str) -> dict[str, Any] | None:
    init_db()
    with conn() as c:
        row = c.execute("SELECT * FROM operators WHERE callsign=?", ((call or "").strip().upper(),)).fetchone()
    return dict(row) if row else None


def operator_login(call: str, password: str) -> str:
    """Connexion d'un opérateur avec SON mot de passe.

    Renvoie ``invalid`` (indicatif incorrect), ``bad`` (mot de passe vide ou
    faux), ``weak`` (mot de passe trop simple à la création), ``created``
    (compte créé et actif), ``pending`` (compte à valider par un
    administrateur), ``disabled`` (compte désactivé) ou ``ok``.
    """
    cs = (call or "").strip().upper()
    if not valid_callsign(cs):
        return "invalid"
    if not password:
        return "bad"
    row = get_operator(cs)
    if row is not None and row.get("password_hash"):
        if not verify_password(password, row["password_hash"]):
            return "bad"
        if row.get("status") == "pending":
            return "pending"
        if not row.get("active"):
            return "disabled"
        return "ok"
    if not password_is_strong(password):
        return "weak"
    # Première connexion : le mot de passe saisi devient celui du compte.
    # Un indicatif inconnu attend l'accord d'un admin si la validation est active ;
    # un opérateur déjà dans la liste a déjà été approuvé en y étant ajouté.
    status = "pending" if (row is None and operator_approval()) else "active"
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO operators(callsign, name, active, password_hash, is_admin, status, created_at) "
            "VALUES (?, '', 1, ?, 0, ?, ?) "
            "ON CONFLICT(callsign) DO UPDATE SET password_hash=excluded.password_hash, "
            "status=excluded.status, active=1",
            (cs, hash_password(password), status, now),
        )
    maybe_backup()
    return "pending" if status == "pending" else "created"


def set_operator_password_for(call: str, password: str) -> None:
    """Mot de passe d'un opérateur, posé par un administrateur."""
    cs = (call or "").strip().upper()
    if not password:
        raise ValueError(_("mot de passe vide"))
    if not password_is_strong(password):
        raise ValueError(password_rule())
    if get_operator(cs) is None:
        raise ValueError(_("indicatif inconnu"))
    with conn() as c:
        c.execute("UPDATE operators SET password_hash=? WHERE callsign=?", (hash_password(password), cs))
    maybe_backup()


def clear_operator_password(call: str) -> None:
    """Oubli de mot de passe : le compte en choisira un neuf à la prochaine connexion."""
    with conn() as c:
        c.execute("UPDATE operators SET password_hash='' WHERE callsign=?", ((call or "").strip().upper(),))
    maybe_backup()


def approve_operator(call: str) -> None:
    with conn() as c:
        c.execute("UPDATE operators SET status='active', active=1 WHERE callsign=?",
                  ((call or "").strip().upper(),))
    maybe_backup()


def set_operator_admin(call: str, is_admin: bool) -> None:
    """Droits d'administration (Réglages) d'un opérateur."""
    cs = (call or "").strip().upper()
    if get_operator(cs) is None:
        raise ValueError(_("indicatif inconnu"))
    with conn() as c:
        c.execute("UPDATE operators SET is_admin=? WHERE callsign=?", (1 if is_admin else 0, cs))
    maybe_backup()


def set_operator_active(call: str, active: bool) -> None:
    with conn() as c:
        c.execute("UPDATE operators SET active=? WHERE callsign=?",
                  (1 if active else 0, (call or "").strip().upper()))
    maybe_backup()


def operator_is_admin(call: str) -> bool:
    row = get_operator(call)
    return bool(row and row.get("is_admin") and row.get("active") and row.get("status", "active") == "active")


def _seed_operators() -> list[str]:
    ops = activation_config().get("operators") or []
    return [str(o).upper() for o in ops if o]


# ── Connexion / schéma ─────────────────────────────────────────────────────


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


_schema_ready: set[str] = set()  # bases déjà créées/migrées (par chemin)


def _snapshot(c: sqlite3.Connection, tag: str) -> Path:
    """Copie cohérente de la base ouverte (hors rotation si tag ≠ activation)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{tag}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.sqlite"
    dst = sqlite3.connect(dest)
    try:
        c.backup(dst)
    finally:
        dst.close()
    return dest


def _seed_station(c: sqlite3.Connection) -> None:
    """Première fiche d'indicatif, créée depuis la section ``activation`` de config.yml."""
    if c.execute("SELECT 1 FROM stations LIMIT 1").fetchone():
        return
    cfg = activation_config()
    cs = str(cfg.get("callsign") or "TM0ABC").strip().upper()
    hero: dict[str, str] = {}
    hero.update({k: str(cfg[k]) for k in ("badge", "subtitle", "flags") if cfg.get(k)})
    grid = str(cfg.get("my_gridsquare") or "").strip().upper()
    if not (_RE_LOCATOR.match(grid) or _RE_LOCATOR8.match(grid)):
        grid = ""
    c.execute(
        "INSERT INTO stations(callsign, slug, label, gridsquare, public, badge, subtitle, flags, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cs, slugify_call(cs), str(cfg.get("label") or ""), grid, 1 if cfg.get("public") else 0,
         hero.get("badge", ""), hero.get("subtitle", ""), hero.get("flags", ""), int(time.time())),
    )


def init_db() -> None:
    """Crée le schéma et applique les migrations (une seule fois par base)."""
    if str(DB_PATH) in _schema_ready:
        return
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS stations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                callsign TEXT UNIQUE NOT NULL,
                slug TEXT UNIQUE NOT NULL,     -- page publique /<slug>
                label TEXT DEFAULT '',
                gridsquare TEXT DEFAULT '',    -- locator de la station (4/6/8 car.)
                start_date TEXT DEFAULT '',    -- AAAA-MM-JJ (affichage)
                end_date TEXT DEFAULT '',
                public INTEGER DEFAULT 0,
                badge TEXT DEFAULT '',         -- bandeau : ex. « 60 ANS »
                subtitle TEXT DEFAULT '',      -- bandeau : ligne sous le libellé
                flags TEXT DEFAULT '',         -- bandeau : drapeaux « fr,be »
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS operators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                callsign TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                password_hash TEXT DEFAULT '',  -- option « un mot de passe par opérateur »
                is_admin INTEGER DEFAULT 0,     -- accès aux Réglages avec son propre mot de passe
                status TEXT DEFAULT 'active',   -- active / pending (validation par un admin)
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station TEXT DEFAULT '',
                source TEXT DEFAULT 'manual',   -- « log » : déduit des QSO enregistrés
                operator_call TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_slot_start ON slots(start_utc);
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station TEXT DEFAULT '',
                slot_id INTEGER,
                operator_call TEXT NOT NULL,
                call TEXT NOT NULL,
                qso_date TEXT NOT NULL,
                time_on TEXT NOT NULL,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                freq_mhz REAL,
                rst_sent TEXT DEFAULT '',
                rst_rcvd TEXT DEFAULT '',
                gridsquare TEXT DEFAULT '',
                sat_name TEXT DEFAULT '',
                comment TEXT DEFAULT '',
                created_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_contact_call ON contacts(call);
            CREATE INDEX IF NOT EXISTS idx_contact_date ON contacts(qso_date DESC);
            CREATE TABLE IF NOT EXISTS callbook (
                call TEXT PRIMARY KEY,
                status TEXT NOT NULL,          -- ok / notfound / error
                fname TEXT DEFAULT '',
                name TEXT DEFAULT '',
                grid TEXT DEFAULT '',
                country TEXT DEFAULT '',
                dxcc INTEGER,
                dxcc_name TEXT DEFAULT '',
                cqzone INTEGER,
                fetched_at INTEGER NOT NULL
            );
            """
        )
        # Migrations légères des bases déjà créées.
        cols = {row[1] for row in c.execute("PRAGMA table_info(contacts)").fetchall()}
        if "sat_name" not in cols:
            c.execute("ALTER TABLE contacts ADD COLUMN sat_name TEXT DEFAULT ''")
        if "source" not in {row[1] for row in c.execute("PRAGMA table_info(slots)").fetchall()}:
            c.execute("ALTER TABLE slots ADD COLUMN source TEXT DEFAULT 'manual'")
        # Photo de la fiche QRZ (vignette montrée pendant la saisie du log).
        # ``image_at`` : date du dernier passage CHERCHANT la photo. Les fiches
        # d'avant cette fonction valent « ok » mais n'ont jamais eu de photo :
        # sans ce repère, elles ne seraient plus jamais réinterrogées.
        book_cols = {row[1] for row in c.execute("PRAGMA table_info(callbook)").fetchall()}
        for col, decl in (("image", "TEXT DEFAULT ''"), ("image_at", "INTEGER DEFAULT 0")):
            if col not in book_cols:
                c.execute(f"ALTER TABLE callbook ADD COLUMN {col} {decl}")
        # Comptes opérateurs (mot de passe individuel, admin, validation).
        ops_cols = {row[1] for row in c.execute("PRAGMA table_info(operators)").fetchall()}
        for col, decl in (("password_hash", "TEXT DEFAULT ''"), ("is_admin", "INTEGER DEFAULT 0"),
                          ("status", "TEXT DEFAULT 'active'")):
            if col not in ops_cols:
                c.execute(f"ALTER TABLE operators ADD COLUMN {col} {decl}")
        # Passage au multi-indicatif : copie intacte de la base AVANT de toucher
        # au schéma (premigration-*.sqlite, hors rotation des sauvegardes).
        missing = [
            t for t in ("slots", "contacts")
            if "station" not in {row[1] for row in c.execute(f"PRAGMA table_info({t})").fetchall()}
        ]
        if missing:
            _snapshot(c, "premigration")
            for table in missing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN station TEXT DEFAULT ''")
        c.execute("CREATE INDEX IF NOT EXISTS idx_contact_station ON contacts(station)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_slot_station ON slots(station)")
        _seed_station(c)
        # Créneaux / QSO d'avant le multi-indicatif → première fiche (TM25TEST).
        first = c.execute("SELECT callsign FROM stations ORDER BY id LIMIT 1").fetchone()[0]
        for table in ("slots", "contacts"):
            c.execute(f"UPDATE {table} SET station=? WHERE station IS NULL OR station=''", (first,))
        # Liste des opérateurs initialisée depuis la config au premier init (idempotent).
        for cs in _seed_operators():
            c.execute(
                "INSERT OR IGNORE INTO operators(callsign, name, active, created_at) "
                "VALUES (?, '', 1, ?)",
                (cs, int(time.time())),
            )
    _schema_ready.add(str(DB_PATH))


# ── Validation / temps ─────────────────────────────────────────────────────


def valid_callsign(value: str) -> bool:
    return bool(_RE_CALLSIGN.match((value or "").strip().upper()))


def valid_locator(value: str) -> bool:
    return not value or bool(_RE_LOCATOR.match(value.strip().upper()))


def paris_local_to_utc_iso(value: str) -> str | None:
    """'YYYY-MM-DDTHH:MM' saisi en heure de Paris → ISO UTC minute.

    Renvoie None si le format est invalide.
    """
    if not value:
        return None
    try:
        naive = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    aware = naive.replace(tzinfo=PARIS)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def utc_iso_to_paris(value: str) -> datetime | None:
    try:
        naive = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, AttributeError):
        return None
    return naive.replace(tzinfo=UTC).astimezone(PARIS)


def now_utc_parts() -> tuple[str, str]:
    """(qso_date 'YYYYMMDD', time_on 'HHMM') à l'instant présent en UTC."""
    now = datetime.now(UTC)
    return now.strftime("%Y%m%d"), now.strftime("%H%M")


# ── Affichage local / UTC ──────────────────────────────────────────────────
# Le stockage reste TOUJOURS en UTC. Ces helpers ne pilotent que l'affichage et
# la saisie. Le « mode » vaut "utc", "local" (fuseau de la station, config.yml)
# ou directement un fuseau IANA — celui du visiteur, que son navigateur annonce
# (« Europe/Brussels », « America/New_York »…) : un chasseur canadien lit les
# créneaux à son heure sans rien régler.

TZ_MODES = ("local", "utc")


def station_tz() -> ZoneInfo:
    """Fuseau de la station (config.yml : site.timezone), Paris par défaut."""
    name = str(site_config().get("timezone") or "").strip()
    return _zone(name) or PARIS


def _zone(name: str) -> ZoneInfo | None:
    """ZoneInfo d'un nom IANA, None s'il est inconnu (cache mémoire)."""
    key = (name or "").strip()
    if not key or len(key) > 64:
        return None
    if key not in _ZONES:
        try:
            _ZONES[key] = ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            _ZONES[key] = None
    return _ZONES[key]


_ZONES: dict[str, ZoneInfo | None] = {}


def valid_tz(name: str) -> bool:
    """Nom de fuseau IANA utilisable ? (ce que renvoie le navigateur)"""
    return _zone(name) is not None


def tzinfo_for(mode: str) -> ZoneInfo | timezone:
    if mode == "utc":
        return UTC
    return _zone(mode) or station_tz()


def tz_label(mode: str) -> str:
    """Étiquette courte du fuseau : « UTC », « Paris », « New York »."""
    if mode == "utc":
        return "UTC"
    name = mode if _zone(mode) else str(site_config().get("timezone") or "Europe/Paris")
    return name.split("/")[-1].replace("_", " ")


def disp(utc_iso: str, mode: str = "local") -> datetime | None:
    """ISO UTC 'YYYY-MM-DDTHH:MM' → datetime aware dans le fuseau d'affichage."""
    try:
        naive = datetime.strptime((utc_iso or "").strip()[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, AttributeError):
        return None
    return naive.replace(tzinfo=UTC).astimezone(tzinfo_for(mode))


def contact_disp(qso_date: str, time_on: str, mode: str = "local") -> datetime | None:
    """(qso_date 'YYYYMMDD', time_on 'HHMM' en UTC) → datetime aware affichage."""
    try:
        combo = f"{(qso_date or '').strip()}{(time_on or '').strip().ljust(4, '0')[:4]}"
        naive = datetime.strptime(combo[:12], "%Y%m%d%H%M")
    except (ValueError, AttributeError):
        return None
    return naive.replace(tzinfo=UTC).astimezone(tzinfo_for(mode))


def input_to_utc_iso(value: str, mode: str = "local") -> str | None:
    """Saisie 'datetime-local' interprétée dans le fuseau `mode` → ISO UTC minute."""
    if not value:
        return None
    try:
        naive = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=tzinfo_for(mode)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def now_input(mode: str = "local") -> str:
    """Instant présent au format d'un <input type=datetime-local> dans `mode`."""
    return datetime.now(tzinfo_for(mode)).strftime("%Y-%m-%dT%H:%M")


def parts_to_utc_iso(qso_date: str, time_on: str) -> str | None:
    """(qso_date « AAAAMMJJ », time_on « HHMM ») → « AAAA-MM-JJTHH:MM » UTC."""
    minutes = _utc_minutes(qso_date, time_on)
    return _minutes_to_iso(minutes) if minutes is not None else None


def utc_iso_to_parts(utc_iso: str) -> tuple[str, str] | None:
    """ISO UTC 'YYYY-MM-DDTHH:MM' → (qso_date 'YYYYMMDD', time_on 'HHMM')."""
    try:
        dt = datetime.strptime((utc_iso or "")[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return None
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M")


# ── Opérateurs ─────────────────────────────────────────────────────────────


def add_operator(call: str, name: str = "") -> None:
    cs = (call or "").strip().upper()
    if not valid_callsign(cs):
        raise ValueError(_("indicatif invalide"))
    init_db()
    with conn() as c:
        c.execute(
            "INSERT INTO operators(callsign, name, active, created_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(callsign) DO UPDATE SET name=excluded.name, active=1",
            (cs, name.strip(), int(time.time())),
        )
    maybe_backup()


def list_operators(active_only: bool = True) -> list[dict[str, Any]]:
    init_db()
    sql = "SELECT * FROM operators"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY callsign"
    with conn() as c:
        return [dict(r) for r in c.execute(sql).fetchall()]


def active_operators() -> list[str]:
    """Opérateurs qui utilisent RÉELLEMENT l'indicatif : présents dans un créneau
    (passé / en cours / futur) ou dans un QSO loggé. Exclut les inscrits inactifs."""
    init_db()
    with conn() as c:
        st = (callsign(),)
        ops = {r[0] for r in c.execute("SELECT DISTINCT operator_call FROM slots WHERE station=?", st)}
        ops |= {r[0] for r in c.execute("SELECT DISTINCT operator_call FROM contacts WHERE station=?", st)}
    return sorted(o for o in ops if o)


# ── Créneaux ───────────────────────────────────────────────────────────────


def slot_conflicts(start_utc: str, end_utc: str, band: str, exclude_id: int | None = None) -> list[dict[str, Any]]:
    """Créneaux existants (indicatif en cours) qui chevauchent [start, end[ sur la même bande."""
    init_db()
    sql = (
        "SELECT * FROM slots WHERE station=? AND band=? AND start_utc < ? AND end_utc > ?"
    )
    params: list[Any] = [callsign(), band.upper(), end_utc, start_utc]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    sql += " ORDER BY start_utc"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def add_slot(
    operator_call: str, start_utc: str, end_utc: str, band: str, mode: str, note: str = ""
) -> int:
    cs = (operator_call or "").strip().upper()
    if not valid_callsign(cs):
        raise ValueError(_("indicatif opérateur invalide"))
    if not start_utc or not end_utc or end_utc <= start_utc:
        raise ValueError(_("créneau invalide (fin ≤ début)"))
    init_db()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO slots(station, operator_call, start_utc, end_utc, band, mode, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (callsign(), cs, start_utc, end_utc, band.upper(), mode.upper(), note.strip(), int(time.time())),
        )
        new_id = int(cur.lastrowid)
    maybe_backup()
    return new_id


def list_slots(upcoming_only: bool = False, station: str | None = None) -> list[dict[str, Any]]:
    """Créneaux d'un indicatif (défaut : celui en cours)."""
    init_db()
    sql = "SELECT * FROM slots WHERE station=?"
    params: list[Any] = [_st(station)]
    if upcoming_only:
        sql += " AND end_utc >= ?"
        params.append(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M"))
    sql += " ORDER BY start_utc"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def get_slot(slot_id: int) -> dict[str, Any] | None:
    init_db()
    with conn() as c:
        row = c.execute("SELECT * FROM slots WHERE id=?", (int(slot_id),)).fetchone()
    return dict(row) if row else None


def update_slot(
    slot_id: int, operator_call: str, start_utc: str, end_utc: str,
    band: str, mode: str, note: str = "",
) -> None:
    cs = (operator_call or "").strip().upper()
    if not valid_callsign(cs):
        raise ValueError(_("indicatif opérateur invalide"))
    if not start_utc or not end_utc or end_utc <= start_utc:
        raise ValueError(_("créneau invalide (fin ≤ début)"))
    init_db()
    with conn() as c:
        c.execute(
            "UPDATE slots SET operator_call=?, start_utc=?, end_utc=?, band=?, mode=?, note=? WHERE id=?",
            (cs, start_utc, end_utc, band.upper(), mode.upper(), note.strip(), int(slot_id)),
        )
    maybe_backup()


def delete_slot(slot_id: int) -> None:
    init_db()
    with conn() as c:
        c.execute("DELETE FROM slots WHERE id=?", (int(slot_id),))
    maybe_backup()


def conflicting_slot_ids() -> set[int]:
    """Ids des créneaux qui en chevauchent un autre sur la MÊME bande."""
    slots = list_slots()
    bad: set[int] = set()
    for i, a in enumerate(slots):
        for b in slots[i + 1:]:
            if a["band"] != b["band"]:
                continue
            if a["start_utc"] < b["end_utc"] and b["start_utc"] < a["end_utc"]:
                bad.add(a["id"])
                bad.add(b["id"])
    return bad


def live_slots(station: str | None = None) -> list[dict[str, Any]]:
    """TOUS les créneaux en cours (start <= maintenant < end) — plusieurs possibles."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    return [s for s in list_slots(station=station) if s["start_utc"] <= now < s["end_utc"]]


def future_slots(station: str | None = None) -> list[dict[str, Any]]:
    """Créneaux à venir STRICTEMENT (start > maintenant) — exclut ceux en cours."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    return [s for s in list_slots(station=station) if s["start_utc"] > now]


def past_slots(station: str | None = None) -> list[dict[str, Any]]:
    """Activations terminées (end <= maintenant), plus récentes d'abord."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    done = [s for s in list_slots(station=station) if s["end_utc"] <= now]
    return sorted(done, key=lambda s: s["start_utc"], reverse=True)


# Heure UTC d'un QSO (qso_date « AAAAMMJJ » + time_on « HHMM ») au format des
# créneaux (« AAAA-MM-JJTHH:MM »), pour comparer les deux en SQL.
_SQL_QSO_UTC = (
    "substr(c.qso_date,1,4) || '-' || substr(c.qso_date,5,2) || '-' || substr(c.qso_date,7,2)"
    " || 'T' || substr(c.time_on,1,2) || ':' || substr(c.time_on,3,2)"
)


def slot_qso_counts(station: str | None = None) -> dict[int, int]:
    """QSO loggés par créneau : même opérateur, même bande, même mode, du début
    à la fin **incluse**. Renvoie {id du créneau: nombre de QSO}.

    Les QSO sont horodatés à la minute : un contact noté à 19:15 a eu lieu
    pendant la minute 19:15, donc il appartient au créneau qui finit à 19:15
    (c'est souvent le dernier QSO du passage satellite, celui qui clôt la
    séance). Si deux créneaux du même opérateur se touchent à cette
    minute-là, le QSO est compté dans le plus récent — celui qui vient de
    commencer — et jamais deux fois.
    """
    st = _st(station)
    init_db()
    with conn() as c:
        slots = c.execute(
            "SELECT id, operator_call, band, mode, start_utc, end_utc FROM slots WHERE station = ?",
            (st,),
        ).fetchall()
        contacts = c.execute(
            f"SELECT operator_call, band, mode, {_SQL_QSO_UTC} AS utc FROM contacts c WHERE c.station = ?",
            (st,),
        ).fetchall()
    counts = {int(s["id"]): 0 for s in slots}
    by_key: dict[tuple[str, str, str], list[Any]] = {}
    for slot in slots:
        by_key.setdefault((slot["operator_call"], slot["band"], slot["mode"]), []).append(slot)
    for qso in contacts:
        holding = [s for s in by_key.get((qso["operator_call"], qso["band"], qso["mode"]), [])
                   if s["start_utc"] <= qso["utc"] <= s["end_utc"]]
        if holding:
            counts[int(max(holding, key=lambda s: s["start_utc"])["id"])] += 1
    return counts


# ── Créneaux déduits du log ────────────────────────────────────────────────
# Un opérateur qui oublie de réserver, ou qui dépasse l'heure prévue, ne perd
# rien : les QSO enregistrés font foi. On regroupe les QSO d'un même opérateur
# sur une même bande et un même mode tant qu'ils sont espacés de moins de
# SESSION_GAP_MIN, puis on crée le créneau manquant ou on étire celui qui
# existe (jamais on ne le raccourcit : l'intention du planning est gardée).

SESSION_GAP_MIN = 30          # au-delà, c'est une autre séance de trafic
SLOT_ATTACH_MIN = 60          # séance rattachée à un créneau proche (dépassement)
SLOT_ROUNDING_MIN = 15        # créneaux calés sur le quart d'heure


def auto_slots() -> bool:
    """Créer et ajuster les créneaux d'après le log (réglage, activé par défaut)."""
    return get_flag("auto_slots", True)


# Vignettes de créneaux sur la page publique : 0 = toutes (défaut). Un chiffre
# limite les « Prochaines activations » ET les « Activations passées ».
PUBLIC_SLOTS_MAX = 200            # garde-fou : au-delà la page devient illisible


def public_slots_max() -> int:
    """Nombre de vignettes de créneaux montrées publiquement (0 = toutes)."""
    return _clamp_int(load_settings().get("public_slots_max", 0), 0, PUBLIC_SLOTS_MAX, 0)


def set_public_slots_max(value: Any) -> int:
    data = load_settings()
    data["public_slots_max"] = _clamp_int(value, 0, PUBLIC_SLOTS_MAX, 0)
    _save_settings(data)
    return data["public_slots_max"]


def _minutes_to_iso(minutes: int) -> str:
    return datetime.fromtimestamp(minutes * 60, UTC).strftime("%Y-%m-%dT%H:%M")


def _floor_to(minutes: int, step: int) -> int:
    return minutes - (minutes % step)


def _ceil_to(minutes: int, step: int) -> int:
    return minutes if minutes % step == 0 else minutes + (step - minutes % step)


def log_sessions(station: str | None = None) -> list[dict[str, Any]]:
    """Séances de trafic lues dans le log : (opérateur, bande, mode, début, fin)."""
    init_db()
    with conn() as c:
        rows = c.execute(
            "SELECT operator_call, band, mode, qso_date, time_on FROM contacts WHERE station=?",
            (_st(station),),
        ).fetchall()
    grouped: dict[tuple[str, str, str], list[int]] = {}
    for r in rows:
        minutes = _utc_minutes(r["qso_date"], r["time_on"])
        if minutes is None or not r["operator_call"]:
            continue
        grouped.setdefault((r["operator_call"], r["band"], r["mode"]), []).append(minutes)
    sessions = []
    for (op, band, mode), times in grouped.items():
        times.sort()
        start = previous = times[0]
        for minute in times[1:]:
            if minute - previous > SESSION_GAP_MIN:
                sessions.append({"operator_call": op, "band": band, "mode": mode,
                                 "first": start, "last": previous})
                start = minute
            previous = minute
        sessions.append({"operator_call": op, "band": band, "mode": mode,
                         "first": start, "last": previous})
    return sorted(sessions, key=lambda s: s["first"])


def slot_lock() -> bool:
    """Interdire de loguer sur une bande/mode réservés par un autre opérateur
    (réglage, activé par défaut) : deux stations ne peuvent pas émettre en même
    temps sous le même indicatif, sur la même bande et le même mode."""
    return get_flag("slot_lock", True)


def blocking_slot(operator_call: str, band: str, mode: str, when_utc: str | None = None,
                  station: str | None = None) -> dict[str, Any] | None:
    """Créneau d'un AUTRE opérateur couvrant cette bande, ce mode et cet instant."""
    cs = (operator_call or "").strip().upper()
    moment = when_utc or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    for slot in list_slots(station=station):
        if (slot["operator_call"] != cs and slot["band"] == (band or "").upper()
                and slot["mode"] == (mode or "").upper()
                and slot["start_utc"] <= moment < slot["end_utc"]):
            return slot
    return None


def reconcile_slots_from_log(station: str | None = None, force: bool = False) -> dict[str, int]:
    """Crée les créneaux oubliés et étire ceux qui ont débordé. {créés, étirés}.

    ``force`` : l'admin le demande depuis les Réglages, on le fait même si le
    rattrapage automatique est décoché (bouton « Mettre à jour maintenant »).
    """
    if not (force or auto_slots()):
        return {"created": 0, "extended": 0}
    st = _st(station)
    created = extended = 0
    slots = list_slots(station=st)
    for session in log_sessions(st):
        start = _minutes_to_iso(_floor_to(session["first"], SLOT_ROUNDING_MIN))
        end = _minutes_to_iso(_ceil_to(session["last"] + 1, SLOT_ROUNDING_MIN))
        # Le créneau est rattaché s'il chevauche la séance ou s'en approche à
        # moins de SLOT_ATTACH_MIN : trafiquer une heure après l'heure prévue
        # prolonge le créneau réservé au lieu d'en créer un autre.
        near_start = _minutes_to_iso(session["first"] - SLOT_ATTACH_MIN)
        near_end = _minutes_to_iso(session["last"] + SLOT_ATTACH_MIN)
        same = [s for s in slots
                if s["operator_call"] == session["operator_call"] and s["band"] == session["band"]
                and s["mode"] == session["mode"] and s["start_utc"] <= near_end and s["end_utc"] >= near_start]
        if not same:
            with conn() as c:
                c.execute(
                    "INSERT INTO slots(station, source, operator_call, start_utc, end_utc, band, mode, "
                    "note, created_at) VALUES (?, 'log', ?, ?, ?, ?, ?, '', ?)",
                    (st, session["operator_call"], start, end, session["band"], session["mode"],
                     int(time.time())),
                )
            created += 1
            slots = list_slots(station=st)
            continue
        slot = same[0]
        new_start, new_end = min(slot["start_utc"], start), max(slot["end_utc"], end)
        if (new_start, new_end) != (slot["start_utc"], slot["end_utc"]):
            with conn() as c:
                c.execute("UPDATE slots SET start_utc=?, end_utc=? WHERE id=?",
                          (new_start, new_end, slot["id"]))
            extended += 1
            slots = list_slots(station=st)
    if created or extended:
        maybe_backup()
    return {"created": created, "extended": extended}


def current_and_next_slot() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    current = nxt = None
    for s in list_slots():
        if s["start_utc"] <= now < s["end_utc"] and current is None:
            current = s
        elif s["start_utc"] > now and nxt is None:
            nxt = s
    return current, nxt


# ── Contacts (QSO) ─────────────────────────────────────────────────────────


def is_dupe(call: str, band: str, mode: str) -> bool:
    init_db()
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM contacts WHERE station=? AND call=? AND band=? AND mode=? LIMIT 1",
            (callsign(), (call or "").strip().upper(), band.upper(), mode.upper()),
        ).fetchone()
    return row is not None


def worked_before(call: str, station: str | None = None) -> dict[str, Any]:
    """Station déjà contactée ? (affiché pendant la saisie du log)

    Pour l'indicatif en cours : nombre de QSO, couples bande/mode déjà faits et
    date du dernier contact. Signale aussi les QSO faits sous les AUTRES
    indicatifs spéciaux du club, qui ne sont pas des doublons.
    """
    cs = (call or "").strip().upper()
    empty = {"call": cs, "worked": 0, "band_modes": [], "last": "", "elsewhere": []}
    if not valid_callsign(cs):
        return empty
    init_db()
    st = _st(station)
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT band, mode, qso_date, time_on, operator_call FROM contacts "
            "WHERE station=? AND call=? ORDER BY qso_date DESC, time_on DESC", (st, cs)).fetchall()]
        others = [dict(r) for r in c.execute(
            "SELECT station, COUNT(*) AS n FROM contacts WHERE station<>? AND call=? GROUP BY station "
            "ORDER BY station", (st, cs)).fetchall()]
    seen: list[str] = []
    for r in rows:
        pair = f"{r['band']} {r['mode']}".strip()
        if pair and pair not in seen:
            seen.append(pair)
    last = ""
    if rows:
        d, t = rows[0]["qso_date"], rows[0]["time_on"]
        last = f"{d[6:8]}/{d[4:6]}/{d[2:4]} {t[:2]}:{t[2:4]}" if len(d) == 8 and len(t) >= 4 else d
    return {"call": cs, "worked": len(rows), "band_modes": seen, "last": last,
            "elsewhere": [{"station": o["station"], "n": o["n"]} for o in others]}


def add_contact(
    *,
    call: str,
    band: str,
    mode: str,
    operator_call: str,
    qso_date: str | None = None,
    time_on: str | None = None,
    slot_id: int | None = None,
    freq_mhz: float | None = None,
    rst_sent: str = "",
    rst_rcvd: str = "",
    gridsquare: str = "",
    sat_name: str = "",
    comment: str = "",
    station: str | None = None,
) -> int:
    cs = (call or "").strip().upper()
    if not valid_callsign(cs):
        raise ValueError(_("indicatif contacté invalide"))
    op = (operator_call or "").strip().upper()
    if not valid_callsign(op):
        raise ValueError(_("indicatif opérateur invalide"))
    grid = (gridsquare or "").strip().upper()
    if not valid_locator(grid):
        raise ValueError(_("locator invalide"))
    if not qso_date or not time_on:
        qso_date, time_on = now_utc_parts()
    init_db()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO contacts(station, slot_id, operator_call, call, qso_date, time_on, band, "
            "mode, freq_mhz, rst_sent, rst_rcvd, gridsquare, sat_name, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _st(station), slot_id, op, cs, qso_date, time_on, band.upper(), mode.upper(),
                freq_mhz, rst_sent.strip(), rst_rcvd.strip(), grid,
                (sat_name or "").strip().upper(), comment.strip(),
                int(time.time()),
            ),
        )
        new_id = int(cur.lastrowid)
    # Le log fait foi : créneau oublié créé, créneau dépassé étiré.
    reconcile_slots_from_log(station)
    maybe_backup()
    return new_id


# Champs modifiables d'un contact (édition).
_EDITABLE = (
    "call", "band", "mode", "qso_date", "time_on", "freq_mhz",
    "rst_sent", "rst_rcvd", "gridsquare", "sat_name", "comment", "operator_call",
)


def update_contact(contact_id: int, **fields: Any) -> None:
    """Met à jour les champs fournis d'un QSO existant (avec validation)."""
    data: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in _EDITABLE or v is None:
            continue
        if k in ("call", "operator_call"):
            v = str(v).strip().upper()
            if not valid_callsign(v):
                raise ValueError(_("{field} invalide", field=k))
        elif k == "gridsquare":
            v = str(v).strip().upper()
            if not valid_locator(v):
                raise ValueError(_("locator invalide"))
        elif k in ("band", "mode", "sat_name"):
            v = str(v).strip().upper()
        elif k == "freq_mhz":
            v = v if isinstance(v, (int, float)) else None
        else:
            v = str(v).strip()
        data[k] = v
    if not data:
        return
    init_db()
    sets = ", ".join(f"{k}=?" for k in data)
    params = list(data.values()) + [int(contact_id)]
    with conn() as c:
        c.execute(f"UPDATE contacts SET {sets} WHERE id=?", params)
    maybe_backup()


def list_contacts(limit: int | None = None, station: str | None = None) -> list[dict[str, Any]]:
    """QSO d'un indicatif (défaut : celui en cours), plus récents d'abord."""
    init_db()
    sql = "SELECT * FROM contacts WHERE station=? ORDER BY qso_date DESC, time_on DESC, id DESC"
    params: list[Any] = [_st(station)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def contacts_for_call(call: str, station: str | None = None) -> dict[str, Any]:
    """Recherche publique : QSO d'un indicatif avec l'activation + score concours.

    Renvoie le détail des contacts et les compteurs utiles au concours
    (total, nombre de bandes, de modes, et de couples bande+mode distincts —
    la métrique « activations » d'une station spéciale).
    """
    cs = (call or "").strip().upper()
    if not valid_callsign(cs):
        return {"call": cs, "found": False, "contacts": [], "total": 0,
                "bands": [], "modes": [], "band_modes": 0}
    init_db()
    with conn() as c:
        rows = [
            dict(r) for r in c.execute(
                "SELECT * FROM contacts WHERE station=? AND call=? ORDER BY qso_date, time_on",
                (_st(station), cs),
            ).fetchall()
        ]
    bands = sorted({r["band"] for r in rows if r["band"]})
    modes = sorted({r["mode"] for r in rows if r["mode"]})
    band_modes = {(r["band"], r["mode"]) for r in rows}
    return {
        "call": cs, "found": bool(rows), "contacts": rows, "total": len(rows),
        "bands": bands, "modes": modes, "band_modes": len(band_modes),
    }


def get_contact(contact_id: int) -> dict[str, Any] | None:
    init_db()
    with conn() as c:
        row = c.execute("SELECT * FROM contacts WHERE id=?", (int(contact_id),)).fetchone()
    return dict(row) if row else None


def contacts_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    """QSO dont l'id est dans ``ids`` (export d'une sélection), ordre chronologique."""
    wanted = sorted({int(i) for i in ids})
    out: list[dict[str, Any]] = []
    init_db()
    with conn() as c:
        for start in range(0, len(wanted), 500):  # limite de variables SQLite
            chunk = wanted[start:start + 500]
            marks = ",".join("?" * len(chunk))
            out += [dict(r) for r in c.execute(f"SELECT * FROM contacts WHERE id IN ({marks})", chunk)]
    return sorted(out, key=lambda q: (q["qso_date"], q["time_on"], q["id"]))


def delete_contact(contact_id: int) -> None:
    init_db()
    with conn() as c:
        c.execute("DELETE FROM contacts WHERE id=?", (int(contact_id),))
    maybe_backup()


# ── Statistiques ───────────────────────────────────────────────────────────


def stats(station: str | None = None) -> dict[str, Any]:
    """Compteurs d'un indicatif (défaut : celui en cours)."""
    st = (_st(station),)
    init_db()
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM contacts WHERE station=?", st).fetchone()[0]
        by_band = [
            {"band": r["band"], "n": r["n"]}
            for r in c.execute(
                "SELECT band, COUNT(*) n FROM contacts WHERE station=? GROUP BY band ORDER BY n DESC", st
            ).fetchall()
        ]
        by_mode = [
            {"mode": r["mode"], "n": r["n"]}
            for r in c.execute(
                "SELECT mode, COUNT(*) n FROM contacts WHERE station=? GROUP BY mode ORDER BY n DESC", st
            ).fetchall()
        ]
        by_op = [
            {"operator_call": r["operator_call"], "n": r["n"]}
            for r in c.execute(
                "SELECT operator_call, COUNT(*) n FROM contacts WHERE station=? "
                "GROUP BY operator_call ORDER BY n DESC", st
            ).fetchall()
        ]
    return {"total": int(total), "by_band": by_band, "by_mode": by_mode, "by_op": by_op}


# ── Cadence de trafic (jauges du log) ──────────────────────────────────────
# « Combien j'en fais à l'heure ? » : le compteur qui donne envie d'enchaîner.
# Deux fenêtres : l'heure écoulée (tendance de fond) et les 10 dernières
# minutes (le pile-up du moment), chacune comparée à la fenêtre précédente.

RATE_FULL_SCALE = 60        # QSO/h correspondant à une jauge pleine
RATE_LEVELS = (             # seuils en QSO/h → libellé affiché
    (0, N_("station calme")),
    (6, N_("ça démarre")),
    (18, N_("bon rythme")),
    (36, N_("ça chauffe")),
    (60, N_("pile-up !")),
)


def _count_between(c: sqlite3.Connection, station: str, operator: str,
                   start: datetime, end: datetime) -> int:
    """QSO enregistrés dans [start, end[ (bornes UTC)."""
    sql = ("SELECT COUNT(*) FROM contacts WHERE station=? "
           "AND (qso_date || substr(time_on, 1, 4)) >= ? "
           "AND (qso_date || substr(time_on, 1, 4)) < ?")
    params: list[Any] = [station, start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M")]
    if operator:
        sql += " AND operator_call = ?"
        params.append(operator)
    return int(c.execute(sql, params).fetchone()[0])


def _rate_level(per_hour: float) -> str:
    """Libellé de la cadence (traduit à l'affichage)."""
    label = RATE_LEVELS[0][1]
    for threshold, text in RATE_LEVELS:
        if per_hour >= threshold:
            label = text
    return label


def qso_rate(operator: str = "", station: str | None = None) -> dict[str, Any]:
    """Cadence de trafic d'un opérateur (ou de la station si ``operator`` est vide).

    Renvoie, pour l'heure écoulée et pour les 10 dernières minutes, le nombre de
    QSO, la cadence ramenée à l'heure, la variation par rapport à la période
    précédente et le remplissage de la jauge (0 à 100).
    """
    st = _st(station)
    op = (operator or "").strip().upper()
    now = datetime.now(UTC)
    init_db()
    windows = {}
    with conn() as c:
        for name, minutes in (("hour", 60), ("ten", 10)):
            # Borne haute à la minute suivante : le QSO qu'on vient d'enregistrer
            # (même minute que « maintenant ») doit compter tout de suite.
            recent = _count_between(c, st, op, now - timedelta(minutes=minutes),
                                    now + timedelta(minutes=1))
            before = _count_between(c, st, op, now - timedelta(minutes=2 * minutes),
                                    now - timedelta(minutes=minutes))
            per_hour = recent * 60 / minutes
            windows[name] = {
                "qsos": recent, "previous": before, "per_hour": round(per_hour, 1),
                "delta": recent - before,
                "trend": "up" if recent > before else ("down" if recent < before else "flat"),
                "gauge": min(100, round(per_hour * 100 / RATE_FULL_SCALE)),
                "level": _rate_level(per_hour),
            }
    windows["operator"] = op
    windows["full_scale"] = RATE_FULL_SCALE
    return windows


def worked_entities(station: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Entités DXCC contactées, la plus récemment travaillée en tête.

    Le code du drapeau vient du préfixe de l'indicatif (aucun réseau
    nécessaire, la vignette est servie par l'application) ; le nom du pays vient
    du callbook QRZ quand il est connu, sinon de la table des préfixes. Les
    indicatifs dont l'entité est inconnue sont ignorés.
    """
    init_db()
    st = (_st(station),)
    with conn() as c:
        rows = c.execute(
            "SELECT call, COUNT(*) AS n, MAX(qso_date || time_on) AS last FROM contacts "
            "WHERE station=? GROUP BY call", st
        ).fetchall()
        known = {r["call"]: (r["dxcc_name"] or r["country"] or "").strip()
                 for r in c.execute("SELECT call, dxcc_name, country FROM callbook").fetchall()}
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        _key, code, name = dxcc_flags.entity_key(row["call"], known.get(row["call"], ""))
        if not code:
            continue
        item = seen.setdefault(code, {"code": code, "name": name or known.get(row["call"], ""),
                                      "n": 0, "last": "", "calls": 0, "from_qrz": False})
        item["n"] += int(row["n"])
        item["calls"] += 1
        item["last"] = max(item["last"], row["last"] or "")
        # Un nom venu de QRZ est plus précis que celui de la table des préfixes.
        qrz_name = known.get(row["call"], "")
        if qrz_name and not item["from_qrz"]:
            item["name"], item["from_qrz"] = qrz_name, True
    out = sorted(seen.values(), key=lambda e: e["last"], reverse=True)
    return out[:limit] if limit else out


# ── Callbook QRZ (nom / locator / DXCC des indicatifs contactés) ───────────
# Cache permanent des lookups QRZ XML : chaque indicatif n'est interrogé
# qu'une fois (à la saisie dans le log, ou par la tâche de fond qui passe
# doucement sur les indicatifs pas encore connus). Seuls indicatif, locator
# et DXCC sont publiés ; nom et prénom restent dans l'espace opérateurs.

CALLBOOK_RETRY_NOTFOUND = 24 * 3600  # inconnu de QRZ : on retente le lendemain
CALLBOOK_RETRY_ERROR = 15 * 60       # erreur réseau / session : on retente plus tôt

logger = logging.getLogger(__name__)


# Compte QRZ : par défaut celui de config.yml (section ``qrz``) ; l'admin peut
# en saisir un autre dans les Réglages (ex. celui du radio-club). Il est gardé
# à part, lisible par le seul service (0600), jamais affiché ni copié dans les
# sauvegardes téléchargeables.
QRZ_ACCOUNT_FILE = ROOT / "var" / "activation_qrz.json"
_RE_QRZ_USER = re.compile(r"^[A-Za-z0-9_.@/-]{3,40}$")
_qrz_own: QrzXmlClient | None = None
_qrz_own_lock = threading.Lock()


def _own_qrz_account() -> dict[str, str]:
    """Compte saisi dans les Réglages ({} si aucun)."""
    try:
        data = json.loads(QRZ_ACCOUNT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    user, pwd = data.get("username"), data.get("password")
    if isinstance(user, str) and isinstance(pwd, str) and user and pwd:
        return {"username": user, "password": pwd}
    return {}


def qrz_account() -> dict[str, str]:
    """Compte QRZ en service : identifiant et source (``settings``, ``config`` ou "")."""
    own = _own_qrz_account()
    if own:
        return {"username": own["username"], "source": "settings"}
    cfg = qrz_config()
    user = cfg.get("username") or cfg.get("xml_username")
    if user and (cfg.get("password") or cfg.get("xml_password")):
        return {"username": str(user), "source": "config"}
    return {"username": "", "source": ""}


def valid_qrz_username(username: str) -> bool:
    return bool(_RE_QRZ_USER.match(username or ""))


def check_qrz_account(username: str, password: str) -> tuple[str, str]:
    """Essaie de se connecter à QRZ : voir ``QrzXmlClient.check_login``."""
    return QrzXmlClient(username, password).check_login()


def set_qrz_account(username: str, password: str) -> None:
    username = username.strip()
    if not valid_qrz_username(username) or not password:
        raise ValueError(_("compte QRZ incomplet"))
    QRZ_ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QRZ_ACCOUNT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"username": username, "password": password}), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(QRZ_ACCOUNT_FILE)


def clear_qrz_account() -> None:
    """Retire le compte des Réglages : retour au compte de config.yml (s'il existe)."""
    QRZ_ACCOUNT_FILE.unlink(missing_ok=True)


def own_qrz_password(username: str) -> str:
    """Mot de passe déjà enregistré pour cet identifiant (champ laissé vide = inchangé)."""
    own = _own_qrz_account()
    return own["password"] if own and own["username"].lower() == username.strip().lower() else ""


def qrz_client() -> Any:
    """Client QRZ XML : compte des Réglages, sinon celui du site (None si aucun)."""
    global _qrz_own
    own = _own_qrz_account()
    if not own:
        return get_shared_client()
    with _qrz_own_lock:
        if _qrz_own is None or (_qrz_own.username, _qrz_own.password) != (own["username"], own["password"]):
            _qrz_own = QrzXmlClient(own["username"], own["password"])
        return _qrz_own


def locator_center(grid: str) -> tuple[float, float] | None:
    """Centre d'un locator 4, 6 ou 8 caractères → (lat, lon), None si invalide."""
    g = (grid or "").strip().upper()
    if not (_RE_LOCATOR.match(g) or _RE_LOCATOR8.match(g)):
        return None
    lon = (ord(g[0]) - 65) * 20.0 - 180.0 + int(g[2]) * 2.0
    lat = (ord(g[1]) - 65) * 10.0 - 90.0 + int(g[3])
    d_lon, d_lat = 2.0, 1.0
    if len(g) >= 6:
        lon += (ord(g[4]) - 65) * (2.0 / 24.0)
        lat += (ord(g[5]) - 65) * (1.0 / 24.0)
        d_lon, d_lat = 2.0 / 24.0, 1.0 / 24.0
    if len(g) == 8:
        d_lon, d_lat = d_lon / 10.0, d_lat / 10.0
        lon += int(g[6]) * d_lon
        lat += int(g[7]) * d_lat
    return round(lat + d_lat / 2.0, 4), round(lon + d_lon / 2.0, 4)


def distance_km(grid_a: str, grid_b: str) -> float | None:
    """Distance orthodromique (km) entre les centres de deux locators."""
    a, b = locator_center(grid_a), locator_center(grid_b)
    if a is None or b is None:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def callbook_get(call: str) -> dict[str, Any] | None:
    init_db()
    with conn() as c:
        row = c.execute(
            "SELECT * FROM callbook WHERE call=?", ((call or "").strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def _callbook_put(call: str, status: str, rec: Any = None) -> None:
    """Enregistre le résultat d'un lookup (``rec`` = XmlLookup si status ok)."""
    grid = (getattr(rec, "grid", "") or "")[:6].upper()
    if not _RE_LOCATOR.match(grid):
        grid = ""
    country = (getattr(rec, "country", "") or "").strip()
    land = (getattr(rec, "land", "") or "").strip()
    init_db()
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO callbook(call, status, fname, name, grid, country, "
            "dxcc, dxcc_name, cqzone, image, image_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call, status,
                (getattr(rec, "fname", "") or "").strip(),
                (getattr(rec, "lname", "") or "").strip(),
                grid, country,
                _int_or_none(getattr(rec, "dxcc", None)),
                land or country,
                _int_or_none(getattr(rec, "cqzone", None)),
                _photo_url(getattr(rec, "image", "")),
                int(time.time()),          # photo cherchée : on ne repassera pas
                int(time.time()),
            ),
        )


def _photo_url(url: str) -> str:
    """Adresse de la photo QRZ, gardée seulement si elle est sûre.

    On ne retient qu'une URL https vers qrz.com : la vignette est affichée dans
    l'espace opérateurs, pas question d'y charger n'importe quel domaine."""
    clean = (url or "").strip()
    if not clean.lower().startswith("https://"):
        return ""
    host = clean.split("/", 3)[2].lower().split(":")[0]
    ok = host == "qrz.com" or host.endswith(".qrz.com")
    return clean[:300] if ok and len(clean) < 300 else ""


def _callbook_fresh(row: dict[str, Any] | None) -> bool:
    """La fiche dispense-t-elle de réinterroger QRZ ?"""
    if row is None:
        return False
    if row["status"] == "ok":
        # Fiche antérieure à la vignette : on la relit UNE fois pour la photo.
        return bool(row.get("image_at"))
    ttl = CALLBOOK_RETRY_NOTFOUND if row["status"] == "notfound" else CALLBOOK_RETRY_ERROR
    return time.time() - (row["fetched_at"] or 0) < ttl


def qrz_lookup(call: str, client: Any) -> dict[str, Any] | None:
    """Fiche callbook d'un indicatif : cache SQLite, sinon QRZ XML.

    Renvoie la ligne ``callbook`` si l'indicatif est connu de QRZ, None sinon
    (inconnu, erreur, ou pas de client). Un portable (``DL1ABC/P``) introuvable
    tel quel est recherché sous son indicatif de base.
    """
    cs = (call or "").strip().upper()
    if not valid_callsign(cs):
        return None
    row = callbook_get(cs)
    if client is not None and not _callbook_fresh(row):
        rec, status = client.lookup_with_status(cs)
        if status == "notfound" and "/" in cs:
            rec, status = client.lookup_with_status(cs.split("/", 1)[0])
        _callbook_put(cs, status, rec)
        row = callbook_get(cs)
    return row if row and row["status"] == "ok" else None


def pending_callbook_calls(limit: int = 1) -> list[str]:
    """Indicatifs contactés sans fiche callbook valable (jamais vus d'abord).

    Comprend les fiches d'avant la vignette QRZ : elles sont correctes mais
    n'ont jamais eu de photo, la tâche de fond les repasse une fois."""
    init_db()
    now = int(time.time())
    with conn() as c:
        rows = c.execute(
            "SELECT ct.call FROM contacts ct LEFT JOIN callbook cb ON cb.call = ct.call "
            "WHERE cb.call IS NULL "
            "OR (cb.status = 'notfound' AND cb.fetched_at < ?) "
            "OR (cb.status = 'error' AND cb.fetched_at < ?) "
            "OR (cb.status = 'ok' AND COALESCE(cb.image_at, 0) = 0) "
            "GROUP BY ct.call ORDER BY cb.call IS NOT NULL, MIN(ct.id) LIMIT ?",
            (now - CALLBOOK_RETRY_NOTFOUND, now - CALLBOOK_RETRY_ERROR, int(limit)),
        ).fetchall()
    return [r[0] for r in rows]


def enrich_one(client: Any) -> str | None:
    """Interroge QRZ pour UN indicatif en attente. Renvoie le statut obtenu
    (``ok`` / ``notfound`` / ``error``), ou None s'il n'y avait rien à faire."""
    if client is None:
        return None
    pending = pending_callbook_calls(1)
    if not pending:
        return None
    qrz_lookup(pending[0], client)
    row = callbook_get(pending[0])
    return row["status"] if row else None


def callbook_progress() -> dict[str, int]:
    """Avancement de l'enrichissement QRZ (affiché dans les Réglages)."""
    init_db()
    with conn() as c:
        stations = int(c.execute("SELECT COUNT(DISTINCT call) FROM contacts").fetchone()[0])
        counts = {
            r[0]: int(r[1])
            for r in c.execute(
                "SELECT status, COUNT(*) FROM callbook "
                "WHERE call IN (SELECT call FROM contacts) GROUP BY status"
            ).fetchall()
        }
    ok, notfound = counts.get("ok", 0), counts.get("notfound", 0)
    return {"stations": stations, "ok": ok, "notfound": notfound,
            "pending": stations - ok - notfound}


# Tâche de fond : un indicatif toutes les `qrz_interval_seconds` (défaut 10 s)
# pour ne pas charger QRZ ; pause longue quand il n'y a rien à faire ou que
# QRZ répond mal. Le service tourne sur un seul worker uvicorn → un seul thread.

_enricher_stop = threading.Event()
_enricher_thread: threading.Thread | None = None


def _enricher_loop(interval: float) -> None:
    while not _enricher_stop.is_set():
        delay = 60.0                  # rien en attente : on repasse dans 1 min
        try:
            status = enrich_one(qrz_client())
            if status == "error":
                delay = 300.0         # QRZ en panne / session refusée : on lève le pied
            elif status is not None:
                delay = interval
        except Exception:  # noqa: BLE001 — la tâche de fond ne doit jamais mourir
            logger.exception("enrichissement QRZ : erreur inattendue")
            delay = 300.0
        _enricher_stop.wait(delay)


def start_enricher() -> None:
    """Démarre l'enrichissement QRZ en tâche de fond (idempotent).

    Désactivable par ``activation.qrz_enrich: false`` dans config.yml.
    """
    global _enricher_thread
    cfg = activation_config()
    if not cfg.get("qrz_enrich", True):
        return
    if _enricher_thread is not None and _enricher_thread.is_alive():
        return
    interval = max(3.0, float(cfg.get("qrz_interval_seconds", 10)))
    _enricher_stop.clear()
    _enricher_thread = threading.Thread(
        target=_enricher_loop, args=(interval,), name="activation-qrz", daemon=True
    )
    _enricher_thread.start()


def stop_enricher() -> None:
    _enricher_stop.set()


# ── Board public : carte, DXCC, classement ─────────────────────────────────


GRID_MISMATCH_KM = 500  # saisi à plus de 500 km de QRZ → saisie tenue pour fausse


def bearing_deg(grid_from: str, grid_to: str) -> float | None:
    """Azimut vrai (0 = nord, 90 = est) entre les centres de deux locators.

    Sert à la boussole de la page de log : d'un coup d'œil, l'opérateur sait
    où tourner l'antenne."""
    start, end = locator_center(grid_from), locator_center(grid_to)
    if start is None or end is None:
        return None
    lat1, lon1 = math.radians(start[0]), math.radians(start[1])
    lat2, lon2 = math.radians(end[0]), math.radians(end[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def call_grids(station: str | None = None) -> dict[str, str]:
    """Locator retenu pour chaque indicatif contacté ("" si inconnu).

    Le plus récent saisi dans un QSO, sinon celui de QRZ. Celui de QRZ
    l'emporte quand le locator saisi n'a que 4 caractères du même carré,
    finit par « AA » (valeur par défaut qu'inventent certains logiciels de log
    à partir du préfixe) ou tombe à plus de ``GRID_MISMATCH_KM`` de celui de
    QRZ (ex. locator de la station précédente resté dans le logiciel).
    Sert à la carte et aux points de distance ; le log n'est pas modifié.
    """
    init_db()
    with conn() as c:
        rows = c.execute(
            "SELECT ct.call, ct.gridsquare, cb.grid AS qrz_grid FROM contacts ct "
            "LEFT JOIN callbook cb ON cb.call = ct.call AND cb.status = 'ok' "
            "WHERE ct.station = ? ORDER BY ct.qso_date DESC, ct.time_on DESC, ct.id DESC",
            (_st(station),),
        ).fetchall()
    logged: dict[str, str] = {}
    from_qrz: dict[str, str] = {}
    for r in rows:
        if r["gridsquare"] and r["call"] not in logged:
            logged[r["call"]] = r["gridsquare"].upper()
        if r["qrz_grid"]:
            from_qrz[r["call"]] = r["qrz_grid"].upper()
    out: dict[str, str] = {}
    for call in sorted({r["call"] for r in rows}):
        grid = logged.get(call) or from_qrz.get(call) or ""
        qrz = from_qrz.get(call, "")
        if qrz and (
            (len(grid) == 4 and qrz.startswith(grid))    # QRZ précise le même carré
            or (len(grid) == 6 and grid.endswith("AA"))  # « xxxxAA » : rempli par un logiciel
            or (distance_km(grid, qrz) or 0) > GRID_MISMATCH_KM  # saisie manifestement fausse
        ):
            grid = qrz
        out[call] = grid if locator_center(grid) else ""
    return out


def map_data(station: str | None = None) -> dict[str, Any]:
    """Stations contactées pour la carte publique : un point par locator, bande
    et mode.

    Locator : voir ``call_grids``. Position = CENTRE du locator (jamais les
    coordonnées précises de QRZ) et aucune donnée nominative : indicatif +
    locator seulement. Les points d'un même carré sont écartés à l'affichage
    (voir static/js/activation-map.js) pour rester tous visibles.
    """
    st = _st(station)
    grids = call_grids(st)
    init_db()
    with conn() as c:
        rows = c.execute(
            "SELECT call, band, mode, COUNT(*) AS n, MAX(qso_date || time_on) AS last "
            "FROM contacts WHERE station = ? GROUP BY call, band, mode", (st,)
        ).fetchall()
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        grid = grids.get(row["call"], "")
        if not grid:
            continue
        band, mode = (row["band"] or "").upper(), (row["mode"] or "").upper()
        key = (grid, band, mode)
        point = groups.get(key)
        if point is None:
            lat, lon = locator_center(grid)
            point = groups[key] = {"grid": grid, "lat": lat, "lon": lon, "band": band,
                                   "mode": mode, "calls": [], "qsos": 0, "last": ""}
        point["calls"].append(row["call"])
        point["qsos"] += int(row["n"])
        point["last"] = max(point["last"], row["last"] or "")
    for point in groups.values():
        point["calls"].sort()
    located = sum(1 for grid in grids.values() if grid)
    points = sorted(groups.values(), key=lambda p: (p["grid"], p["band"], p["mode"]))
    return {"points": points, "stations": len(grids), "located": located,
            "bands": sorted({p["band"] for p in points if p["band"]}),
            "modes": sorted({p["mode"] for p in points if p["mode"]})}


def _entity_name(call: str) -> str:
    """Nom de l'entité DXCC déduit du préfixe ("" si le préfixe est inconnu)."""
    return dxcc_flags.entity_for_call(call)[1]


def satellite_stats(station: str | None = None) -> dict[str, Any]:
    """QSO passés par satellite, détaillés par satellite.

    ``by_sat`` : [{"sat", "n", "stations"}] du plus travaillé au moins
    travaillé ; ``total`` : nombre de QSO satellite ; ``share`` : leur part du
    log. Le nom est celui saisi dans le log (une coquille comme « F0-29 »
    apparaît donc telle quelle — c'est ainsi qu'on la repère).
    """
    st = _st(station)
    init_db()
    with conn() as c:
        rows = c.execute(
            "SELECT upper(trim(sat_name)) AS sat, COUNT(*) AS n, COUNT(DISTINCT call) AS stations "
            "FROM contacts WHERE station = ? AND trim(sat_name) != '' "
            "GROUP BY sat ORDER BY n DESC, sat", (st,)
        ).fetchall()
        total_log = c.execute("SELECT COUNT(*) FROM contacts WHERE station = ?", (st,)).fetchone()[0]
    by_sat = [{"sat": r["sat"], "n": int(r["n"]), "stations": int(r["stations"])} for r in rows]
    total = sum(item["n"] for item in by_sat)
    return {"by_sat": by_sat, "total": total, "count": len(by_sat),
            "share": round(100 * total / total_log, 1) if total_log else 0.0}


def qso_timeline(station: str | None = None) -> dict[str, Any]:
    """Rythme de l'activité : QSO par jour et par heure UTC.

    ``by_day`` (chronologique), ``by_hour`` (24 valeurs, 0 h → 23 h UTC),
    la meilleure journée, la meilleure heure d'horloge (toutes journées
    confondues), le meilleur créneau d'une heure précise, le nombre d'heures
    où la station a été active et la moyenne de QSO sur ces heures-là.
    """
    st = _st(station)
    init_db()
    with conn() as c:
        rows = c.execute(
            "SELECT qso_date AS day, substr(time_on, 1, 2) AS hour, COUNT(*) AS n "
            "FROM contacts WHERE station = ? GROUP BY day, hour ORDER BY day, hour", (st,)
        ).fetchall()
    days: dict[str, int] = {}
    hours = [0] * 24
    best_slot = {"day": "", "hour": "", "n": 0}
    for row in rows:
        days[row["day"]] = days.get(row["day"], 0) + int(row["n"])
        try:
            hour = int(row["hour"])
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            hours[hour] += int(row["n"])
        if int(row["n"]) > best_slot["n"]:
            best_slot = {"day": row["day"], "hour": row["hour"], "n": int(row["n"])}
    total = sum(days.values())
    by_day = [{"day": day, "n": n} for day, n in sorted(days.items())]
    best_day = max(by_day, key=lambda d: d["n"], default=None)
    best_hour_n = max(hours, default=0)
    active_hours = len(rows)
    return {
        "by_day": by_day,
        "by_hour": hours,
        "best_day": best_day,
        "best_hour": {"hour": hours.index(best_hour_n) if best_hour_n else 0, "n": best_hour_n},
        "best_slot": best_slot,
        "active_hours": active_hours,
        "per_hour": round(total / active_hours, 1) if active_hours else 0.0,
        "days": len(by_day),
        "total": total,
    }


# ── Périodes de « run » (pile-up) ─────────────────────────────────────────
# Un run, c'est le moment où ça décolle : les QSO s'enchaînent sans temps mort.
# On repère les contacts dont la cadence LOCALE (fenêtre glissante) dépasse un
# seuil, puis on recolle ceux qui se suivent. Le résultat dit quand la station
# a le mieux tourné, et à quelle vitesse.

RUN_WINDOW_MIN = 10        # fenêtre glissante d'observation
RUN_MIN_RATE = 30          # QSO/h : en dessous, ce n'est pas un run
RUN_MIN_QSOS = 5           # une pointe de deux contacts n'est pas un run


def _qso_minutes(station: str) -> list[int]:
    """Instants des QSO, en minutes depuis l'époque, dans l'ordre."""
    init_db()
    with conn() as c:
        rows = c.execute(
            "SELECT qso_date, time_on FROM contacts WHERE station = ? "
            "ORDER BY qso_date, time_on", (station,)
        ).fetchall()
    out = []
    for row in rows:
        day, hhmm = (row["qso_date"] or ""), (row["time_on"] or "")
        if len(day) != 8 or len(hhmm) < 4:
            continue
        try:
            when = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]),
                            int(hhmm[:2]), int(hhmm[2:4]), tzinfo=UTC)
        except ValueError:
            continue
        out.append(int(when.timestamp() // 60))
    return sorted(out)


def run_periods(station: str | None = None, top: int = 5) -> dict[str, Any]:
    """Meilleurs moments de l'activation : périodes où la cadence s'emballe.

    Renvoie ``periods`` (les ``top`` meilleures, cadence décroissante), chacune
    avec son début, sa fin, sa durée, son nombre de QSO et sa cadence, plus
    ``best`` (la meilleure) et le total de QSO passés en run.
    """
    minutes = _qso_minutes(_st(station))
    if not minutes:
        return {"periods": [], "best": None, "total": 0, "share": 0.0}
    hot: list[bool] = []
    half = RUN_WINDOW_MIN / 2.0
    low = high = 0
    for at in minutes:
        # Fenêtre CENTRÉE sur le QSO : avec une fenêtre qui ne regarde que
        # devant, les derniers contacts d'une rafale passaient pour calmes.
        while minutes[low] < at - half:
            low += 1
        while high < len(minutes) and minutes[high] <= at + half:
            high += 1
        hot.append((high - low) * 60.0 / RUN_WINDOW_MIN >= RUN_MIN_RATE)
    runs: list[dict[str, Any]] = []
    current: list[int] = []
    for at, is_hot in zip(minutes, hot):
        if is_hot and (not current or at - current[-1] <= RUN_WINDOW_MIN):
            current.append(at)
            continue
        if len(current) >= RUN_MIN_QSOS:
            runs.append(_run_from(current))
        current = [at] if is_hot else []
    if len(current) >= RUN_MIN_QSOS:
        runs.append(_run_from(current))
    runs.sort(key=lambda r: (-r["rate"], -r["qsos"]))
    in_runs = sum(r["qsos"] for r in runs)
    return {
        "periods": runs[:top] if top else [],
        "best": runs[0] if runs else None,
        "total": in_runs,
        "count": len(runs),
        "share": round(100 * in_runs / len(minutes), 1),
    }


def _run_from(minutes: list[int]) -> dict[str, Any]:
    """Résumé d'une suite de QSO serrés : durée, cadence, début et fin (UTC)."""
    start, end = minutes[0], minutes[-1]
    # Un QSO isolé dure au moins la fenêtre d'observation : sinon la cadence
    # d'une salve de trois contacts dans la même minute serait infinie.
    span = max(end - start, RUN_WINDOW_MIN)
    return {
        "start": datetime.fromtimestamp(start * 60, UTC).strftime("%Y-%m-%dT%H:%M"),
        "end": datetime.fromtimestamp(end * 60, UTC).strftime("%Y-%m-%dT%H:%M"),
        "minutes": end - start,
        "qsos": len(minutes),
        "rate": round(len(minutes) * 60.0 / span, 1),
    }


def dxcc_table(station: str | None = None) -> dict[str, Any]:
    """Entités DXCC contactées : stations et QSO par entité.

    L'entité vient du callbook QRZ quand il la connaît, sinon du **préfixe de
    l'indicatif** : sans compte QRZ (ou avant que le callbook soit rempli), le
    tableau est quand même juste pour l'immense majorité des stations. Seuls
    les indicatifs dont le préfixe est inconnu restent « pas encore
    identifiés ».
    """
    st = _st(station)
    init_db()
    with conn() as c:
        rows = c.execute(
            "SELECT ct.call, COUNT(*) AS qsos, cb.dxcc, cb.dxcc_name FROM contacts ct "
            "LEFT JOIN callbook cb ON cb.call = ct.call AND cb.status = 'ok' AND cb.dxcc_name != '' "
            "WHERE ct.station = ? GROUP BY ct.call", (st,)
        ).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    unidentified = 0
    for row in rows:
        qrz_name = (row["dxcc_name"] or "").strip()
        # Regroupement par entité : le code réunit les variantes de nom
        # (« Germany » côté préfixe, « Fed. Rep. of Germany » côté QRZ) ET les
        # deux sources — un préfixe absent de la table ne doit pas créer une
        # seconde entité, sans drapeau, à côté de la même entité connue.
        key, code, prefix_name = dxcc_flags.entity_key(row["call"], qrz_name)
        if not key:
            unidentified += 1
            continue
        item = groups.setdefault(key, {"dxcc": row["dxcc"], "code": code,
                                       "dxcc_name": qrz_name or prefix_name,
                                       "stations": 0, "qsos": 0, "from_qrz": bool(qrz_name)})
        item["stations"] += 1
        item["qsos"] += int(row["qsos"])
        if qrz_name and not item["from_qrz"]:      # le nom officiel QRZ l'emporte
            item["dxcc_name"], item["dxcc"], item["from_qrz"] = qrz_name, row["dxcc"], True
    entities = sorted(groups.values(),
                      key=lambda e: (-e["stations"], -e["qsos"], e["dxcc_name"]))
    return {"entities": entities, "count": len(entities), "unidentified": unidentified}


# ── Certificats des chasseurs (réglable) ──────────────────────────────────
# Un chasseur qui retrouve ses QSO sur la page publique peut repartir avec son
# certificat en PDF. Désactivé tant que l'admin ne l'a pas voulu : le dessin
# porte le nom du club, autant qu'il le relise avant.

DEFAULT_CERTIFICATE: dict[str, Any] = {
    "enabled": False,    # bouton « Certificat » sur la page publique
    "names": False,      # inscrire le nom du chasseur (sinon : son seul indicatif)
    "ranking": True,     # médaille avec la place au classement
    "mention": "",       # petite ligne libre en bas de page
    "max_qso": 10,       # contacts listés sur la page principale
    "annexe": True,      # page(s) annexe avec le journal complet
    "flag": "auto",      # drapeau du pays : "" (aucun), "auto" (d'après l'indicatif) ou un code
    "border": False,     # fin liseré autour de la page
    "border_colors": ["#0055A4", "#FFFFFF", "#EF3340"],
    "emblem": True,      # emblème pylône + banderole sous le poste de radio
    "emblem_text": "",   # texte de la banderole (nom du club) ; vide → « HAM RADIO »
    "ham_symbol": False, # symbole international du radioamateur (losange)
    "qr_url": "",        # adresse du QR code (colonne de gauche) ; vide → pas de QR
}
_RE_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
CERTIFICATE_MENTION_MAX = 160
CERTIFICATE_EMBLEM_MAX = 40     # au-delà, la banderole deviendrait illisible
CERTIFICATE_QR_MAX = 200        # un QR plus dense ne se lirait plus à 2 cm
_RE_QR_URL = re.compile(r"^https?://[^\s/]+\.[^\s]*$", re.IGNORECASE)


def get_certificate_options() -> dict[str, Any]:
    saved = load_settings().get("certificate") or {}
    d = DEFAULT_CERTIFICATE
    return {
        "enabled": bool(saved.get("enabled", d["enabled"])),
        "names": bool(saved.get("names", d["names"])),
        "ranking": bool(saved.get("ranking", d["ranking"])),
        "mention": str(saved.get("mention") or "")[:CERTIFICATE_MENTION_MAX],
        "max_qso": _clamp_int(saved.get("max_qso"), 1, 14, d["max_qso"]),
        "annexe": bool(saved.get("annexe", d["annexe"])),
        "flag": _clean_flag(saved.get("flag", d["flag"])),
        "border": bool(saved.get("border", d["border"])),
        "border_colors": _clean_colors(saved.get("border_colors"), d["border_colors"]),
        "emblem": bool(saved.get("emblem", d["emblem"])),
        "emblem_text": str(saved.get("emblem_text") or "")[:CERTIFICATE_EMBLEM_MAX],
        "ham_symbol": bool(saved.get("ham_symbol", d["ham_symbol"])),
        "qr_url": _clean_qr_url(saved.get("qr_url")),
    }


def _clean_qr_url(value: Any) -> str:
    """Adresse http(s) du QR code ; « https:// » ajouté s'il manque, sinon rien."""
    url = "".join(str(value or "").split())
    if url and "://" not in url:
        url = "https://" + url
    return url if len(url) <= CERTIFICATE_QR_MAX and _RE_QR_URL.match(url) else ""


def _clean_flag(value: Any) -> str:
    """« auto », un code d'entité connu, ou rien."""
    code = str(value or "").strip().upper()
    if code in ("", "AUTO"):
        return code.lower()
    return code if any(code == c for c, _n in dxcc_flags.PREFIXES.values()) else ""


def _clean_colors(value: Any, default: list[str]) -> list[str]:
    """Trois couleurs #rrggbb ; toute valeur douteuse retombe sur le défaut."""
    given = list(value or [])
    return [given[i] if i < len(given) and _RE_HEX.match(str(given[i] or "")) else default[i]
            for i in range(3)]


def set_certificate_options(form: dict[str, Any]) -> dict[str, Any]:
    data = load_settings()
    data["certificate"] = {
        "enabled": bool(form.get("enabled")),
        "names": bool(form.get("names")),
        "ranking": bool(form.get("ranking")),
        "mention": str(form.get("mention") or "").strip()[:CERTIFICATE_MENTION_MAX],
        "max_qso": _clamp_int(form.get("max_qso"), 1, 14, DEFAULT_CERTIFICATE["max_qso"]),
        "annexe": bool(form.get("annexe")),
        "flag": _clean_flag(form.get("flag")),
        "border": bool(form.get("border")),
        "border_colors": _clean_colors(
            [form.get("border1"), form.get("border2"), form.get("border3")],
            DEFAULT_CERTIFICATE["border_colors"]),
        "emblem": bool(form.get("emblem")),
        "emblem_text": " ".join(str(form.get("emblem_text") or "").split())[:CERTIFICATE_EMBLEM_MAX],
        "ham_symbol": bool(form.get("ham_symbol")),
        "qr_url": _clean_qr_url(form.get("qr_url")),
    }
    _save_settings(data)
    return get_certificate_options()


def certificate_flags() -> list[tuple[str, str]]:
    """Entités disponibles pour le drapeau du certificat : (code, nom), triées."""
    seen = {code: name for code, name in dxcc_flags.PREFIXES.values()}
    return sorted(seen.items(), key=lambda item: item[1])


def certificates_on() -> bool:
    return get_certificate_options()["enabled"]


# ── Fiche du correspondant sur la page de log (réglable) ──────────────────
# Pendant la saisie : la photo de la fiche QRZ et une boussole qui montre où
# tourner l'antenne. Chacune se règle en pixels, 0 = on ne l'affiche pas.

DEFAULT_LOG_VIEW: dict[str, int] = {"photo": 96, "compass": 120}
LOG_VIEW_MAX = 260


def get_log_view() -> dict[str, int]:
    saved = load_settings().get("log_view") or {}
    d = DEFAULT_LOG_VIEW
    return {
        "photo": _clamp_int(saved.get("photo"), 0, LOG_VIEW_MAX, d["photo"]),
        "compass": _clamp_int(saved.get("compass"), 0, LOG_VIEW_MAX, d["compass"]),
    }


def set_log_view(form: dict[str, Any]) -> dict[str, int]:
    data = load_settings()
    data["log_view"] = {
        "photo": _clamp_int(form.get("photo"), 0, LOG_VIEW_MAX, DEFAULT_LOG_VIEW["photo"]),
        "compass": _clamp_int(form.get("compass"), 0, LOG_VIEW_MAX, DEFAULT_LOG_VIEW["compass"]),
    }
    _save_settings(data)
    return get_log_view()


# ── Rapport PDF et logo du club (réglables dans les Réglages) ─────────────

DEFAULT_REPORT: dict[str, Any] = {
    "hours": False,      # « Rythme, heure par heure » : hors du rapport par défaut
    "dxcc_all": True,    # toutes les entités avec leur drapeau (sinon : les dix premières)
    "hunters": 10,       # nombre de chasseurs listés (0 = pas de palmarès)
    "sats": True,        # « Satellites » : masqué de toute façon sans QSO satellite
    "one_page": False,   # tout tenir sur une page, quitte à couper les listes
    "runs": 3,           # meilleurs moments (pile-up) listés ; 0 = section retirée
}
REPORT_HUNTERS_MAX = 100

LOGO_DIR = ROOT / "var" / "branding"
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg"}


def get_report_options() -> dict[str, Any]:
    """Contenu du rapport PDF (sections facultatives)."""
    saved = load_settings().get("report") or {}
    d = DEFAULT_REPORT
    return {
        "hours": bool(saved.get("hours", d["hours"])),
        "dxcc_all": bool(saved.get("dxcc_all", d["dxcc_all"])),
        "hunters": _clamp_int(saved.get("hunters"), 0, REPORT_HUNTERS_MAX, d["hunters"]),
        "sats": bool(saved.get("sats", d["sats"])),
        "one_page": bool(saved.get("one_page", d["one_page"])),
        "runs": _clamp_int(saved.get("runs"), 0, 10, d["runs"]),
    }


def set_report_options(form: dict[str, Any]) -> dict[str, Any]:
    data = load_settings()
    data["report"] = {
        "hours": bool(form.get("hours")),
        "dxcc_all": bool(form.get("dxcc_all")),
        "hunters": _clamp_int(form.get("hunters"), 0, REPORT_HUNTERS_MAX,
                              DEFAULT_REPORT["hunters"]),
        "sats": bool(form.get("sats")),
        "one_page": bool(form.get("one_page")),
        "runs": _clamp_int(form.get("runs"), 0, 10, DEFAULT_REPORT["runs"]),
    }
    _save_settings(data)
    return get_report_options()


def logo_on_pages() -> bool:
    """Le logo est-il affiché sur les pages web (bandeau des indicatifs) ?"""
    return get_flag("logo_on_pages", True)


def logo_path() -> Path | None:
    """Fichier du logo du club (None s'il n'y en a pas)."""
    for ext in LOGO_TYPES:
        candidate = LOGO_DIR / f"logo.{ext}"
        if candidate.is_file():
            return candidate
    return None


def logo_info() -> dict[str, Any] | None:
    """Logo publié : chemin, type, taille et date (pour l'aperçu des Réglages)."""
    path = logo_path()
    if path is None:
        return None
    stat = path.stat()
    return {"path": path, "kind": path.suffix.lstrip("."), "bytes": stat.st_size,
            "mtime": int(stat.st_mtime), "media_type": LOGO_TYPES[path.suffix.lstrip(".")]}


def _image_kind(data: bytes) -> str:
    """« png », « jpg » ou "" : on se fie au CONTENU, pas au nom du fichier."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    return ""


def set_logo(data: bytes) -> str:
    """Enregistre le logo (PNG ou JPEG). Renvoie le type ; ValueError sinon."""
    if not data:
        raise ValueError(_("fichier vide"))
    if len(data) > LOGO_MAX_BYTES:
        raise ValueError(_("image trop lourde (2 Mo maximum)"))
    kind = _image_kind(data)
    if not kind:
        raise ValueError(_("format non reconnu : attendu PNG ou JPEG"))
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    for ext in LOGO_TYPES:          # un seul logo à la fois
        (LOGO_DIR / f"logo.{ext}").unlink(missing_ok=True)
    (LOGO_DIR / f"logo.{kind}").write_bytes(data)
    return kind


def clear_logo() -> None:
    for ext in LOGO_TYPES:
        (LOGO_DIR / f"logo.{ext}").unlink(missing_ok=True)


# ── Style de la carte publique (réglable par l'admin dans les Réglages) ────
# Un point par locator × bande × mode : la COULEUR dit le mode, la FORME dit la
# bande. Les deux tables sont modifiables ; un mode ou une bande absent de la
# table prend la valeur « autres ».

# Ordre choisi pour que deux bandes voisines ne se ressemblent pas : la table
# fait le tour des formes, donc au-delà de 8 bandes une forme resservira.
MAP_SHAPES = ("circle", "diamond", "square", "triangle", "star", "cross", "hexagon", "pentagon")

DEFAULT_MAP_STYLE: dict[str, Any] = {
    "enabled": True,                  # décoché : tous les points identiques
    "filters": True,                  # cases à cocher bande/mode sous la carte
    "mode_colors": {
        "SSB": "#e8543f", "CW": "#f2b134", "FT8": "#2f7fd1", "FT4": "#17a2a2",
        "RTTY": "#8e5bd0", "PSK31": "#d2691e", "FM": "#2fa84f", "AM": "#8a8f98",
        "SSTV": "#e0559c", "DIGI": "#1f6f8b",
    },
    "mode_default": "#5b6b7c",        # modes absents de la table
    "band_shapes": {
        band: MAP_SHAPES[i % len(MAP_SHAPES)] for i, band in enumerate(BANDS)
    },
    "band_default": "circle",         # bandes absentes de la table
}

_RE_COLOR = re.compile(r"^#[0-9a-f]{6}$")


def _clean_color(value: Any, default: str) -> str:
    color = str(value or "").strip().lower()
    return color if _RE_COLOR.match(color) else default


def _clean_shape(value: Any, default: str) -> str:
    shape = str(value or "").strip().lower()
    return shape if shape in MAP_SHAPES else default


def get_map_style(station: str | None = None) -> dict[str, Any]:
    """Couleurs (modes) et formes (bandes) de la carte d'un indicatif."""
    saved = (load_settings().get("map_style_by_station") or {}).get(_st(station)) or {}
    d = DEFAULT_MAP_STYLE
    return {
        "enabled": bool(saved.get("enabled", d["enabled"])),
        "filters": bool(saved.get("filters", d["filters"])),
        "mode_colors": {**d["mode_colors"],
                        **{m: _clean_color(c, d["mode_colors"].get(m, d["mode_default"]))
                           for m, c in (saved.get("mode_colors") or {}).items()}},
        "mode_default": _clean_color(saved.get("mode_default"), d["mode_default"]),
        "band_shapes": {**d["band_shapes"],
                        **{b: _clean_shape(sh, d["band_shapes"].get(b, d["band_default"]))
                           for b, sh in (saved.get("band_shapes") or {}).items()}},
        "band_default": _clean_shape(saved.get("band_default"), d["band_default"]),
    }


def set_map_style(form: dict[str, Any], station: str | None = None) -> dict[str, Any]:
    """Enregistre le style de la carte ; « reset » revient aux valeurs par défaut."""
    data = load_settings()
    key = _st(station)
    if form.get("reset"):
        (data.get("map_style_by_station") or {}).pop(key, None)
        _save_settings(data)
        return get_map_style(key)
    d = DEFAULT_MAP_STYLE
    style = {
        "enabled": bool(form.get("enabled")),
        "filters": bool(form.get("filters")),
        "mode_colors": {m: _clean_color(form.get(f"color_{m}"),
                                        d["mode_colors"].get(m, d["mode_default"]))
                        for m in MODES},
        "mode_default": _clean_color(form.get("color_default"), d["mode_default"]),
        "band_shapes": {b: _clean_shape(form.get(f"shape_{b}"),
                                        d["band_shapes"].get(b, d["band_default"]))
                        for b in BANDS},
        "band_default": _clean_shape(form.get("shape_default"), d["band_default"]),
    }
    data.setdefault("map_style_by_station", {})[key] = style
    _save_settings(data)
    return get_map_style(key)


# ── Règle de points (réglable par l'admin dans les Réglages) ───────────────
# Points d'un QSO = points par contact + points du mode + points de distance
# (chaque critère activable). Score d'un chasseur = somme de ses QSO ; avec
# « un QSO par bande×mode », seul le meilleur QSO de chaque couple compte.

DEFAULT_SCORING: dict[str, Any] = {
    "enabled": False,           # classement aux points (sinon : bande×mode)
    "unique_band_mode": True,   # doublons bande×mode comptés 0
    "per_qso_on": True,
    "per_qso": 1,               # points par contact
    "mode_on": True,
    "mode_points": {"CW": 3, "SSB": 2, "FM": 2, "AM": 2, "RTTY": 2, "SSTV": 2,
                    "FT8": 1, "FT4": 1, "PSK31": 1, "DIGI": 1},
    "mode_default": 1,          # modes non listés
    "distance_on": True,
    "km_per_point": 500,        # 1 point par tranche complète de N km
    "distance_max": 0,          # plafond des points de distance (0 = aucun)
}


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def get_scoring(station: str | None = None) -> dict[str, Any]:
    """Règle de points d'un indicatif (défaut : en cours), complétée par les défauts."""
    saved = (load_settings().get("scoring_by_station") or {}).get(_st(station)) or {}
    rule = {**DEFAULT_SCORING, **{k: v for k, v in saved.items() if k in DEFAULT_SCORING}}
    rule["mode_points"] = {**DEFAULT_SCORING["mode_points"], **(saved.get("mode_points") or {})}
    return rule


def set_scoring(form: dict[str, Any], station: str | None = None) -> dict[str, Any]:
    """Enregistre la règle de points d'un indicatif (défaut : en cours), valeurs bornées."""
    d = DEFAULT_SCORING
    rule = {
        "enabled": bool(form.get("enabled")),
        "unique_band_mode": bool(form.get("unique_band_mode")),
        "per_qso_on": bool(form.get("per_qso_on")),
        "per_qso": _clamp_int(form.get("per_qso"), 0, 1000, d["per_qso"]),
        "mode_on": bool(form.get("mode_on")),
        "mode_points": {
            m: _clamp_int(form.get(f"mode_{m}"), 0, 1000, d["mode_points"].get(m, d["mode_default"]))
            for m in MODES
        },
        "mode_default": _clamp_int(form.get("mode_default"), 0, 1000, d["mode_default"]),
        "distance_on": bool(form.get("distance_on")),
        "km_per_point": _clamp_int(form.get("km_per_point"), 1, 20000, d["km_per_point"]),
        "distance_max": _clamp_int(form.get("distance_max"), 0, 1000, d["distance_max"]),
    }
    data = load_settings()
    data.setdefault("scoring_by_station", {})[_st(station)] = rule
    _save_settings(data)
    return rule


def qso_points(mode: str, km: float | None, rule: dict[str, Any]) -> int:
    """Points d'un QSO selon la règle (distance inconnue → 0 point de distance)."""
    pts = 0
    if rule["per_qso_on"]:
        pts += rule["per_qso"]
    if rule["mode_on"]:
        pts += rule["mode_points"].get(mode, rule["mode_default"])
    if rule["distance_on"] and km is not None:
        dist = int(km // rule["km_per_point"])
        pts += min(dist, rule["distance_max"]) if rule["distance_max"] else dist
    return pts


def scoring_summary(rule: dict[str, Any] | None = None) -> str:
    """Règle de points en une phrase (sous le classement, dans les Réglages)."""
    rule = rule or get_scoring()
    bits = []
    if rule["per_qso_on"]:
        bits.append(_("{n} pts par QSO", n=rule["per_qso"]) if rule["per_qso"] > 1 else _("{n} pt par QSO", n=rule["per_qso"]))
    if rule["mode_on"]:
        by_pts: dict[int, list[str]] = {}
        for m, p in rule["mode_points"].items():
            by_pts.setdefault(p, []).append(m)
        modes = ", ".join(f"{'/'.join(ms)} {p}" for p, ms in sorted(by_pts.items(), reverse=True))
        bits.append(_("mode : {modes}, autres {other}", modes=modes, other=rule["mode_default"]))
    if rule["distance_on"]:
        cap = f" (max {rule['distance_max']})" if rule["distance_max"] else ""
        bits.append(_("distance : 1 pt / {km} km", km=rule["km_per_point"]) + cap)
    if rule["unique_band_mode"]:
        bits.append(_("un seul QSO compté par bande×mode"))
    return " · ".join(bits) or _("aucun critère actif")


def _hunter_sort_key(h: dict[str, Any]) -> tuple:
    # Points (tous à 0 si la règle est désactivée), puis couples bande×mode
    # distincts, QSO, et enfin le premier à avoir atteint ce total.
    return (-h["points"], -h["band_modes"], -h["qsos"], h["last"], h["call"])


def hunters_ranking(limit: int | None = 50, station: str | None = None) -> list[dict[str, Any]]:
    """Classement des chasseurs (stations ayant contacté l'indicatif).

    Aux points si la règle est activée (``get_scoring``), sinon aux couples
    bande×mode distincts. Distance : du locator de la station
    (``my_gridsquare``) à celui retenu pour le chasseur (``call_grids``).
    """
    st = _st(station)
    rule = get_scoring(st)
    scored = rule["enabled"]
    home = my_gridsquare(st)
    grids = call_grids(st) if scored and rule["distance_on"] else {}
    init_db()
    with conn() as c:
        rows = c.execute(
            "SELECT call, band, mode, qso_date, time_on FROM contacts WHERE station=?", (st,)
        ).fetchall()
        dxcc = {
            r["call"]: r["dxcc_name"]
            for r in c.execute("SELECT call, dxcc_name FROM callbook WHERE status = 'ok'")
        }
    by_call: dict[str, list[Any]] = {}
    for r in rows:
        by_call.setdefault(r["call"], []).append(r)
    out = []
    for call, qsos in by_call.items():
        km = distance_km(home, grids.get(call, "")) if grids else None
        best: dict[tuple[str, str], int] = {}
        total = 0
        for q in qsos:
            pts = qso_points(q["mode"], km, rule) if scored else 0
            key = (q["band"], q["mode"])
            best[key] = max(best.get(key, 0), pts)
            total += pts
        if scored and rule["unique_band_mode"]:
            total = sum(best.values())
        out.append({
            "call": call, "qsos": len(qsos),
            "bands": len({q["band"] for q in qsos}), "modes": len({q["mode"] for q in qsos}),
            "band_modes": len(best),
            "last": max(f"{q['qso_date']}{q['time_on']}" for q in qsos),
            "dxcc_name": dxcc.get(call, "") or _entity_name(call),
            "dxcc_code": dxcc_flags.entity_key(call, dxcc.get(call, ""))[1], "points": total,
            "km": round(km) if km is not None else None,
        })
    out.sort(key=_hunter_sort_key)
    for i, h in enumerate(out, 1):
        h["rank"] = i
    return out[:limit] if limit else out


# ── Exports ────────────────────────────────────────────────────────────────


def _adif_field(name: str, value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    return f"<{name.upper()}:{len(s)}>{s}"


def to_adif(contacts: list[dict[str, Any]] | None = None) -> str:
    """Émet un ADIF combiné (STATION_CALLSIGN = indicatif d'activation)."""
    if contacts is None:
        contacts = list_contacts()
    station = callsign()
    grids: dict[str, str] = {}  # locator de chaque indicatif rencontré
    lines = [
        f"ADIF export {station} — {label()}",
        "<ADIF_VER:5>3.1.4",
        "<PROGRAMID:13>tm-activation",
        "<EOH>",
    ]
    for q in contacts:
        st = q.get("station") or station
        if st not in grids:
            grids[st] = my_gridsquare(st)
        parts = [
            _adif_field("CALL", q.get("call")),
            _adif_field("QSO_DATE", q.get("qso_date")),
            _adif_field("TIME_ON", q.get("time_on")),
            _adif_field("BAND", q.get("band")),
            _adif_field("MODE", q.get("mode")),
            _adif_field("FREQ", q.get("freq_mhz")),
            _adif_field("RST_SENT", q.get("rst_sent")),
            _adif_field("RST_RCVD", q.get("rst_rcvd")),
            _adif_field("GRIDSQUARE", q.get("gridsquare")),
            _adif_field("SAT_NAME", q.get("sat_name")),
            _adif_field("PROP_MODE", "SAT" if (q.get("sat_name") or "").strip() else ""),
            _adif_field("OPERATOR", q.get("operator_call")),
            _adif_field("STATION_CALLSIGN", st),
            _adif_field("MY_GRIDSQUARE", grids[st]),
            _adif_field("COMMENT", q.get("comment")),
            "<EOR>",
        ]
        lines.append(" ".join(p for p in parts if p))
    return "\n".join(lines) + "\n"


# ── Import ADIF : aperçu puis import des QSO cochés ────────────────────────
# 1. analyze_adif() donne un statut à chaque QSO du fichier ; 2. l'opérateur
# confirme les QSO cochés (import_rows). Entre les deux, le fichier attend
# dans var/cache sous un jeton aléatoire (purgé au bout d'une heure).

IMPORT_TMP_DIR = ROOT / "var" / "cache" / "activation-import"
IMPORT_TMP_TTL = 3600
IMPORT_TIME_TOLERANCE_MIN = 10  # même QSO à ±10 min (horloges des logiciels)

IMPORT_STATUS = {
    "new": N_("Nouveau"),
    "worked": N_("Déjà contacté sur cette bande/mode"),
    "in_log": N_("Déjà dans le log"),
    "file_dupe": N_("En double dans le fichier"),
    "invalid": N_("Invalide"),
}
IMPORT_DEFAULT_CHECKED = ("new", "worked")

_RE_ADIF_DATE = re.compile(r"^\d{8}$")
_RE_ADIF_TIME = re.compile(r"^\d{4}(\d{2})?$")
_RE_IMPORT_TOKEN = re.compile(r"^[0-9a-f]{32}$")


def _utc_minutes(qso_date: str, time_on: str) -> int | None:
    """(YYYYMMDD, HHMM[SS]) en UTC → minutes depuis l'epoch, None si invalide."""
    try:
        dt = datetime.strptime(f"{qso_date}{(time_on or '')[:4]}", "%Y%m%d%H%M")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=UTC).timestamp()) // 60


def _adif_mode(r: dict[str, str]) -> str:
    mode = (r.get("mode") or "").strip().upper()
    submode = (r.get("submode") or "").strip().upper()
    if mode in ("USB", "LSB"):                   # hors norme mais fréquent
        return "SSB"
    if submode and mode in ("", "MFSK", "PSK"):  # ADIF 3 : FT4 = MFSK / FT4
        return submode
    return mode


def analyze_adif(
    text: str, operator_call: str = "", prefer_file_operator: bool = False,
) -> list[dict[str, Any]]:
    """Analyse un ADIF avant import : un dict par QSO, avec ``status``.

    Tous les QSO sont rattachés à ``operator_call`` ; avec
    ``prefer_file_operator``, le champ ADIF OPERATOR prime quand il est valide
    (sauf s'il vaut l'indicatif d'activation, que certains logiciels y mettent).
    Statuts : voir ``IMPORT_STATUS``. Deux QSO sont « le même » s'ils ont même
    indicatif, bande et mode à ±``IMPORT_TIME_TOLERANCE_MIN`` minutes.
    """
    from app.wavelog_client import parse_adif  # import local : évite un cycle

    default_op = (operator_call or "").strip().upper()
    station = callsign()
    init_db()
    in_log: dict[tuple[str, str, str], list[int]] = {}
    with conn() as c:
        for r in c.execute(
            "SELECT call, band, mode, qso_date, time_on FROM contacts WHERE station=?", (station,)
        ):
            pool = in_log.setdefault((r["call"], r["band"], r["mode"]), [])
            m = _utc_minutes(r["qso_date"], r["time_on"])
            if m is not None:
                pool.append(m)
    in_file: dict[tuple[str, str, str], list[int]] = {}

    def near(minutes: int, pool: list[int]) -> bool:
        return any(abs(minutes - m) <= IMPORT_TIME_TOLERANCE_MIN for m in pool)

    rows: list[dict[str, Any]] = []
    for idx, r in enumerate(parse_adif(text or "")):
        call = (r.get("call") or "").strip().upper()
        band = (r.get("band") or "").strip().upper()
        mode = _adif_mode(r)
        qso_date = (r.get("qso_date") or "").strip()
        time_on = (r.get("time_on") or "").strip()
        grid = (r.get("gridsquare") or "").strip().upper()[:6]
        file_op = (r.get("operator") or "").strip().upper()
        use_file_op = prefer_file_operator and valid_callsign(file_op) and file_op != station
        op = file_op if use_file_op else default_op
        try:
            freq_mhz = float((r.get("freq") or "").replace(",", ".")) if r.get("freq") else None
        except ValueError:
            freq_mhz = None
        minutes = None
        if _RE_ADIF_DATE.match(qso_date) and _RE_ADIF_TIME.match(time_on):
            minutes = _utc_minutes(qso_date, time_on)
        reason = ""
        if not valid_callsign(call):
            reason = _("indicatif invalide")
        elif not band or not mode:
            reason = _("bande ou mode manquant")
        elif minutes is None:
            reason = _("date/heure manquante ou invalide")
        elif not valid_callsign(op):
            reason = _("opérateur à choisir")
        if reason:
            status = "invalid"
        else:
            key = (call, band, mode)
            if near(minutes, in_file.get(key, [])):
                status = "file_dupe"
            elif near(minutes, in_log.get(key, [])):
                status = "in_log"
            elif key in in_log or key in in_file:
                status = "worked"
            else:
                status = "new"
            in_file.setdefault(key, []).append(minutes)
        rows.append({
            "idx": idx, "status": status, "reason": reason,
            "call": call, "band": band, "mode": mode,
            "qso_date": qso_date, "time_on": time_on[:4], "operator_call": op,
            "freq_mhz": freq_mhz,
            "rst_sent": (r.get("rst_sent") or "").strip(),
            "rst_rcvd": (r.get("rst_rcvd") or "").strip(),
            "gridsquare": grid if valid_locator(grid) else "",
            "sat_name": (r.get("sat_name") or "").strip(),
            "comment": (r.get("comment") or "").strip(),
        })
    return rows


def import_rows(rows: list[dict[str, Any]], selected: set[int]) -> dict[str, int]:
    """Importe les QSO analysés dont l'index est coché (jamais les invalides)."""
    added = invalid = 0
    for row in rows:
        if row["status"] == "invalid":
            invalid += 1
            continue
        if row["idx"] not in selected:
            continue
        add_contact(
            call=row["call"], band=row["band"], mode=row["mode"],
            operator_call=row["operator_call"], qso_date=row["qso_date"],
            time_on=row["time_on"], freq_mhz=row["freq_mhz"],
            rst_sent=row["rst_sent"], rst_rcvd=row["rst_rcvd"],
            gridsquare=row["gridsquare"], sat_name=row["sat_name"], comment=row["comment"],
        )
        added += 1
    return {"total": len(rows), "added": added,
            "skipped": len(rows) - added - invalid, "invalid": invalid}


def import_adif(text: str, operator_call: str = "", prefer_file_operator: bool = True) -> dict[str, int]:
    """Import direct, sans aperçu : prend les QSO nouveaux ou déjà contactés
    sur la bande/mode ; ignore ceux déjà dans le log, en double ou invalides."""
    rows = analyze_adif(text, operator_call, prefer_file_operator)
    return import_rows(rows, {r["idx"] for r in rows if r["status"] in IMPORT_DEFAULT_CHECKED})


def stash_import(text: str) -> str:
    """Garde un fichier ADIF le temps de l'aperçu ; renvoie son jeton."""
    IMPORT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in IMPORT_TMP_DIR.glob("*.adi"):
        try:
            if now - old.stat().st_mtime > IMPORT_TMP_TTL:
                old.unlink()
        except OSError:
            pass
    token = secrets.token_hex(16)
    (IMPORT_TMP_DIR / f"{token}.adi").write_text(text, encoding="utf-8")
    return token


def load_import(token: str) -> str | None:
    if not _RE_IMPORT_TOKEN.match(token or ""):
        return None
    try:
        return (IMPORT_TMP_DIR / f"{token}.adi").read_text(encoding="utf-8")
    except OSError:
        return None


def drop_import(token: str) -> None:
    if _RE_IMPORT_TOKEN.match(token or ""):
        (IMPORT_TMP_DIR / f"{token}.adi").unlink(missing_ok=True)


_CSV_COLS = [
    "qso_date", "time_on", "call", "band", "mode", "freq_mhz",
    "rst_sent", "rst_rcvd", "gridsquare", "sat_name", "operator_call", "comment",
]


def to_csv(contacts: list[dict[str, Any]] | None = None) -> str:
    if contacts is None:
        contacts = list_contacts()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_CSV_COLS, extrasaction="ignore")
    w.writeheader()
    for q in contacts:
        w.writerow({k: q.get(k, "") for k in _CSV_COLS})
    return buf.getvalue()


# ── Sauvegardes ────────────────────────────────────────────────────────────
# Snapshots cohérents de la base (API backup SQLite) + copie du mot de passe
# opérateurs, dans var/backups/, avec rotation. Aucune donnée réelle n'est
# jamais supprimée : la rotation ne touche QUE le dossier des snapshots.


def _rotate_backups() -> None:
    for pattern in ("activation-*.sqlite", "password-*.txt", "settings-*.json"):
        files = sorted(BACKUP_DIR.glob(pattern))
        for old in files[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)


def backup_now() -> Path:
    """Écrit un snapshot horodaté de la base (+ mot de passe) et le renvoie."""
    init_db()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"activation-{stamp}.sqlite"
    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)  # snapshot cohérent même en cours d'écriture
        finally:
            dst.close()
    finally:
        src.close()
    # Le mot de passe opérateur vit dans un fichier séparé : on le joint.
    if OP_PASSWORD_FILE.is_file():
        pw_copy = BACKUP_DIR / f"password-{stamp}.txt"
        pw_copy.write_text(OP_PASSWORD_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        pw_copy.chmod(0o600)
    # Les réglages (flags) aussi.
    if SETTINGS_FILE.is_file():
        (BACKUP_DIR / f"settings-{stamp}.json").write_text(
            SETTINGS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
        )
    _rotate_backups()
    global _last_backup_ts
    _last_backup_ts = time.time()
    return dest


def maybe_backup() -> None:
    """Sauvegarde throttlée, appelée après chaque écriture (best-effort)."""
    global _last_backup_ts
    if time.time() - _last_backup_ts < _BACKUP_MIN_INTERVAL:
        return
    try:
        backup_now()
    except Exception:  # noqa: BLE001 — une sauvegarde qui échoue ne doit rien casser
        pass


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_DIR.is_dir():
        return []
    out = []
    for f in sorted(BACKUP_DIR.glob("activation-*.sqlite"), reverse=True):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def last_backup_info() -> dict[str, Any] | None:
    backups = list_backups()
    return backups[0] if backups else None
