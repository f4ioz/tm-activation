"""Activation of a temporary club callsign (e.g. TM25TEST).

Raw SQLite data layer (same style as ``app/db.py``) but in a **separate**
database (``var/activation.sqlite``): the ``f4ioz.sqlite`` database is wiped on
every Wavelog refresh, and the activation log must never be mixed into it.

Tables (slots and QSOs carry the special callsign in the ``station`` column):
- ``stations``  : the club's special callsigns, only one "current" at a time;
- ``operators`` : operators allowed to transmit under the callsign;
- ``slots``     : booked slots (who / when / band / mode);
- ``contacts``  : logged QSOs (worked callsigns);
- ``callbook``  : cache of QRZ records for worked callsigns (name, locator,
  DXCC), filled at logging time and by a gentle background task.

Times are stored in UTC. Slots as ISO ``YYYY-MM-DDTHH:MM``; contacts in ADIF
format (``qso_date`` ``YYYYMMDD`` + ``time_on`` ``HHMM``).
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
BACKUP_KEEP = 40                 # number of snapshots kept (rotation)
_BACKUP_MIN_INTERVAL = 600       # throttle: at most 1 backup / 10 min on writes
_last_backup_ts = 0.0

PARIS = ZoneInfo("Europe/Paris")
UTC = timezone.utc

# Bands / modes offered in the forms (common HF + VHF/UHF).
BANDS = [
    "160M", "80M", "60M", "40M", "30M", "20M", "17M", "15M", "12M", "10M",
    "6M", "4M", "2M", "70CM", "23CM",
]
MODES = ["SSB", "CW", "FT8", "FT4", "RTTY", "PSK31", "FM", "AM", "SSTV", "DIGI"]

_RE_CALLSIGN = re.compile(r"^[A-Z0-9]{3,10}(/[A-Z0-9]{1,4})?$")
_RE_LOCATOR = re.compile(r"^[A-R]{2}\d{2}([A-X]{2})?$")
_RE_LOCATOR8 = re.compile(r"^[A-R]{2}\d{2}[A-X]{2}\d{2}$")  # station (e.g. JN18FS89)


# ── Special callsigns (stations) ───────────────────────────────────────────
# The club activates special callsigns from time to time (TM25TEST, then
# others). Only one is "current" at a time (admin setting): it is the one the
# operators' area logs and schedules; the others remain viewable on their
# public page /<slug>. The operator list, password and QRZ callbook are
# shared by all callsigns.

# Flags drawn in CSS (activation.css) → radio prefix shown next to them.
FLAG_PREFIXES = {"fr": "F", "be": "ON", "de": "DL", "it": "I", "nl": "PA", "lu": "LX", "es": "EA"}
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_SAT_NOTE = re.compile(r"\bsat(?:ellites?|s)?\b", re.I)


def note_is_sat(note: str | None) -> bool:
    """Slot note that mentions a satellite ("SAT FO-29", "QRV Sat SO-50"…)
    → 🛰️ icon in the tiles. Whole word only: "Samedi", "Saturne" excluded."""
    return bool(_RE_SAT_NOTE.search(note or ""))


def slugify_call(call: str) -> str:
    """Callsign → public URL segment: TM25TEST → tm25test."""
    return re.sub(r"[^a-z0-9]", "", (call or "").lower())


def list_stations() -> list[dict[str, Any]]:
    """All special callsigns (most recent first), with their QSO count."""
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
    """Current special callsign (admin setting), else the first one created."""
    st = get_station(load_settings().get("current_station") or "")
    if st is None:
        with conn() as c:  # init_db() already done by get_station
            st = dict(c.execute("SELECT * FROM stations ORDER BY id LIMIT 1").fetchone())
    return st


def _st(station: str | None = None) -> str:
    """Target callsign: the one passed as a parameter, else the current one."""
    return (station or current_station()["callsign"]).strip().upper()


def callsign() -> str:
    """Current special callsign."""
    return current_station()["callsign"]


def label() -> str:
    st = current_station()
    return st["label"] or st["callsign"]


def my_gridsquare(station: str | None = None) -> str:
    """Locator of a special callsign (default: current one) — ADIF and distances."""
    st = get_station(station) if station else current_station()
    return ((st or {}).get("gridsquare") or "").upper()


def is_public() -> bool:
    """Is the current callsign's public page online?"""
    return bool(current_station()["public"])


def station_status(st: dict[str, Any]) -> str:
    """One of "current", "upcoming" (start dated in the future) or "archive"."""
    if st["callsign"] == callsign():
        return "current"
    if st.get("start_date") and st["start_date"] > datetime.now(PARIS).strftime("%Y-%m-%d"):
        return "upcoming"
    return "archive"


def set_current_station(call: str) -> None:
    """Switch the operators' area (log, schedule, ADIF, points) to ``call``."""
    st = get_station(call)
    if st is None:
        raise ValueError(_("indicatif inconnu"))
    data = load_settings()
    data["current_station"] = st["callsign"]
    _save_settings(data)


def _clean_station(fields: dict[str, Any]) -> dict[str, Any]:
    """Editable fields of a record, validated (ValueError otherwise)."""
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
    """New special callsign (it does not become "current" on its own)."""
    cs = (call or "").strip().upper()
    slug = slugify_call(cs)
    # A digit in the slug: a callsign can never shadow a site page (/grid…).
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
    """Update a record. The callsign itself never changes: it signs the QSOs."""
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
    """Delete a record that has NO QSO (its scheduled slots go with it).

    Refused for the current callsign and as soon as any QSO carries this
    callsign: a log is never lost. Full backup right before the deletion.
    """
    st = get_station(call)
    if st is None:
        raise ValueError(_("indicatif inconnu"))
    cs = st["callsign"]
    if cs == callsign():
        raise ValueError(_("{call} est l'indicatif en cours : mets-en un autre en cours d'abord", call=cs))
    backup_now()
    with conn() as c:
        # Checked in the same transaction as the deletion.
        if c.execute("SELECT 1 FROM contacts WHERE station=? LIMIT 1", (cs,)).fetchone():
            raise ValueError(_("{call} a des QSO : sa fiche est conservée", call=cs))
        c.execute("DELETE FROM slots WHERE station=?", (cs,))
        c.execute("DELETE FROM stations WHERE callsign=?", (cs,))
    data = load_settings()
    if cs in (data.get("scoring_by_station") or {}):
        del data["scoring_by_station"][cs]
        _save_settings(data)


# ── Persistent settings (flags toggled from the admin UI) ──────────────────

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
    """Show the contact list on the public board? (default: no)."""
    return get_flag("show_contacts", False)


def show_map_stats() -> bool:
    """Map, DXCC table and ranking on the public board? (default: yes)."""
    return get_flag("show_map_stats", True)


# ── club operator auth ────────────────────────────────────────────────────
# Session dedicated to the TM25TEST area, SEPARATE from the site's private admin mode.
# The token signs "activation:{ts}" (domain separation): an operator cookie
# therefore cannot be reused as an f4ioz_priv admin cookie.

OP_COOKIE = "tm_auth"
OP_TOKEN_TTL = 12 * 3600  # 12 h
OP_PASSWORD_FILE = ROOT / "var" / "activation_password"


def operator_password() -> str:
    """club operators' password.

    The ``var/activation_password`` file takes precedence (settable from the UI);
    otherwise ``activation.password`` from config.yml. Empty → operator auth not
    configured (only the site admin can get in, for bootstrapping).
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
    # Force an immediate backup (the password is critical).
    try:
        backup_now()
    except Exception:  # noqa: BLE001
        pass


def _op_sig(ts: str, call: str = "") -> str:
    payload = f"activation:{ts}:{call}" if call else f"activation:{ts}"
    return hmac.new(_auth.get_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_op_token(call: str = "", now: int | None = None) -> str:
    """Operator session token. With ``call``, it is valid ONLY for that
    callsign: another operator's account cannot be borrowed."""
    ts = str(int(time.time()) if now is None else int(now))
    cs = (call or "").strip().upper()
    return f"{ts}.{cs}.{_op_sig(ts, cs)}" if cs else f"{ts}.{_op_sig(ts)}"


def op_session(token: str | None) -> dict[str, str] | None:
    """Payload of a valid token: {"call": callsign} ("" for the shared password)."""
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
    """Callsign of the logged-in account ("" with the shared password)."""
    sess = op_session(token)
    return sess["call"] if sess else ""


def operator_authed(token: str | None) -> bool:
    """Valid operator session: active account (per-operator password) or
    configured shared password."""
    sess = op_session(token)
    if sess is None:
        return False
    if per_operator_auth():
        row = get_operator(sess["call"]) if sess["call"] else None
        return bool(row and row.get("active") and row.get("status", "active") == "active"
                    and row.get("password_hash"))
    return bool(operator_password())


# ── Operator accounts (option: one password per operator) ──────────────────
# By default, all operators share one password (operator_password).
# "per_operator_auth" setting: everyone logs in with THEIR OWN password, created
# on their first login. "operator_approval" setting: a new account waits for
# an administrator's approval. An operator already in the list (added by an
# admin) has nothing to get approved: they choose their password and get in.

PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    """Salted hash of a password (never stored in clear text)."""
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
    """One password per operator (otherwise: shared password)."""
    return get_flag("per_operator_auth", False)


def operator_approval() -> bool:
    """Accounts created on the fly wait for an administrator's approval."""
    return get_flag("operator_approval", False)


# Requirements for an operator account password (creation and reset),
# adjustable in the Settings: minimum length and number of uppercase letters,
# digits and special characters required (0 = no requirement).
PASSWORD_MIN_LEN = 8              # historical default
PASSWORD_LEN_MAX = 64             # upper bound of the length setting
PASSWORD_COUNT_MAX = 8            # upper bound of the counters (uppercase…)
DEFAULT_PASSWORD_RULE: dict[str, int] = {
    "min_length": PASSWORD_MIN_LEN, "min_upper": 1, "min_digits": 1, "min_special": 1,
}
_RE_UPPER = re.compile(r"[A-ZÀ-Þ]")
_RE_DIGIT = re.compile(r"[0-9]")
_RE_SPECIAL = re.compile(r"[^0-9A-Za-zÀ-ÿ]")


def get_password_rule() -> dict[str, int]:
    """Current requirements for operator passwords."""
    saved = load_settings().get("password_rule") or {}
    d = DEFAULT_PASSWORD_RULE
    return {
        "min_length": _clamp_int(saved.get("min_length"), 4, PASSWORD_LEN_MAX, d["min_length"]),
        "min_upper": _clamp_int(saved.get("min_upper"), 0, PASSWORD_COUNT_MAX, d["min_upper"]),
        "min_digits": _clamp_int(saved.get("min_digits"), 0, PASSWORD_COUNT_MAX, d["min_digits"]),
        "min_special": _clamp_int(saved.get("min_special"), 0, PASSWORD_COUNT_MAX, d["min_special"]),
    }


def set_password_rule(form: dict[str, Any]) -> dict[str, int]:
    """Save the requirements (out-of-range values fall back to the default)."""
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
    """Rule shown on the login page, based on the current setting."""
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


# ── Anti-bot question (no external service, no server-side state) ──────────
# The token signs the time AND the right answer: the signature is re-checked with
# the submitted answer, without keeping the question server-side. A form filled in
# under MIN_FILL_SECONDS, or whose trap field is filled, comes from a bot.

CAPTCHA_TTL = 900          # 15 min to answer
CAPTCHA_MIN_FILL = 2.0     # a human takes more than 2 s to fill in the form
CAPTCHA_TRAP = "website"   # hidden trap field: bots fill it in


def _captcha_sig(ts: str, answer: int) -> str:
    return hmac.new(_auth.get_secret(), f"captcha:{ts}:{answer}".encode(), hashlib.sha256).hexdigest()


def make_captcha() -> dict[str, str]:
    """Simple arithmetic question + signed token ({"question", "token"})."""
    a, b = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
    ts = str(int(time.time()))
    return {"question": f"{a} + {b}", "token": f"{ts}.{_captcha_sig(ts, a + b)}"}


def check_captcha(token: str | None, answer: str | None, trap: str | None = "") -> bool:
    """Correct answer, fresh token, form neither instant nor pre-filled by a bot."""
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
    """Login of an operator with THEIR OWN password.

    Returns ``invalid`` (bad callsign), ``bad`` (empty or wrong password),
    ``weak`` (password too simple at creation), ``created`` (account created
    and active), ``pending`` (account awaiting an administrator's approval),
    ``disabled`` (account disabled) or ``ok``.
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
    # First login: the password entered becomes the account's password.
    # An unknown callsign waits for an admin's approval if approval is enabled;
    # an operator already in the list was approved when they were added to it.
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
    """An operator's password, set by an administrator."""
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
    """Forgotten password: the account will choose a new one at the next login."""
    with conn() as c:
        c.execute("UPDATE operators SET password_hash='' WHERE callsign=?", ((call or "").strip().upper(),))
    maybe_backup()


def approve_operator(call: str) -> None:
    with conn() as c:
        c.execute("UPDATE operators SET status='active', active=1 WHERE callsign=?",
                  ((call or "").strip().upper(),))
    maybe_backup()


def set_operator_admin(call: str, is_admin: bool) -> None:
    """An operator's admin rights (log under any callsign, report).

    Removing admin also removes superadmin: a superadmin is an admin."""
    cs = (call or "").strip().upper()
    if get_operator(cs) is None:
        raise ValueError(_("indicatif inconnu"))
    with conn() as c:
        if is_admin:
            c.execute("UPDATE operators SET is_admin=1 WHERE callsign=?", (cs,))
        else:
            c.execute("UPDATE operators SET is_admin=0, is_superadmin=0 WHERE callsign=?", (cs,))
    maybe_backup()


def set_operator_superadmin(call: str, is_superadmin: bool) -> None:
    """Access to the Settings. Granting superadmin grants admin; revoking it
    leaves the operator an admin."""
    cs = (call or "").strip().upper()
    if get_operator(cs) is None:
        raise ValueError(_("indicatif inconnu"))
    with conn() as c:
        if is_superadmin:
            c.execute("UPDATE operators SET is_admin=1, is_superadmin=1 WHERE callsign=?", (cs,))
        else:
            c.execute("UPDATE operators SET is_superadmin=0 WHERE callsign=?", (cs,))
    maybe_backup()


def set_operator_active(call: str, active: bool) -> None:
    with conn() as c:
        c.execute("UPDATE operators SET active=? WHERE callsign=?",
                  (1 if active else 0, (call or "").strip().upper()))
    maybe_backup()


def _operator_usable(row: dict[str, Any] | None) -> bool:
    return bool(row and row.get("active") and row.get("status", "active") == "active")


def operator_is_admin(call: str) -> bool:
    """Admin or superadmin (the latter includes the former)."""
    row = get_operator(call)
    return _operator_usable(row) and bool(row.get("is_admin") or row.get("is_superadmin"))


def operator_is_superadmin(call: str) -> bool:
    row = get_operator(call)
    return _operator_usable(row) and bool(row.get("is_superadmin"))


def _seed_operators() -> list[str]:
    ops = activation_config().get("operators") or []
    return [str(o).upper() for o in ops if o]


# ── Connection / schema ────────────────────────────────────────────────────


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


_schema_ready: set[str] = set()  # databases already created/migrated (by path)


def _snapshot(c: sqlite3.Connection, tag: str) -> Path:
    """Consistent copy of the open database (outside rotation if tag ≠ activation)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{tag}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.sqlite"
    dst = sqlite3.connect(dest)
    try:
        c.backup(dst)
    finally:
        dst.close()
    return dest


def _seed_station(c: sqlite3.Connection) -> None:
    """First callsign record, created from the ``activation`` section of config.yml."""
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
    """Create the schema and apply migrations (only once per database)."""
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
                is_admin INTEGER DEFAULT 0,     -- log sous tout indicatif, rapport PDF
                is_superadmin INTEGER DEFAULT 0, -- en plus : accès aux Réglages
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
        # Lightweight migrations of existing databases.
        cols = {row[1] for row in c.execute("PRAGMA table_info(contacts)").fetchall()}
        if "sat_name" not in cols:
            c.execute("ALTER TABLE contacts ADD COLUMN sat_name TEXT DEFAULT ''")
        if "source" not in {row[1] for row in c.execute("PRAGMA table_info(slots)").fetchall()}:
            c.execute("ALTER TABLE slots ADD COLUMN source TEXT DEFAULT 'manual'")
        # Photo from the QRZ record (thumbnail shown while logging).
        # ``image_at``: date of the last pass that LOOKED FOR the photo. Records
        # older than this feature count as "ok" but never had a photo: without
        # this marker, they would never be queried again.
        book_cols = {row[1] for row in c.execute("PRAGMA table_info(callbook)").fetchall()}
        for col, decl in (("image", "TEXT DEFAULT ''"), ("image_at", "INTEGER DEFAULT 0")):
            if col not in book_cols:
                c.execute(f"ALTER TABLE callbook ADD COLUMN {col} {decl}")
        # Operator accounts (individual password, admin, approval).
        ops_cols = {row[1] for row in c.execute("PRAGMA table_info(operators)").fetchall()}
        for col, decl in (("password_hash", "TEXT DEFAULT ''"), ("is_admin", "INTEGER DEFAULT 0"),
                          ("status", "TEXT DEFAULT 'active'"),
                          ("is_superadmin", "INTEGER DEFAULT 0")):
            if col not in ops_cols:
                c.execute(f"ALTER TABLE operators ADD COLUMN {col} {decl}")
        # Move to multi-callsign: untouched copy of the database BEFORE touching
        # the schema (premigration-*.sqlite, outside the backup rotation).
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
        # Slots / QSOs from before multi-callsign → first record (TM25TEST).
        first = c.execute("SELECT callsign FROM stations ORDER BY id LIMIT 1").fetchone()[0]
        for table in ("slots", "contacts"):
            c.execute(f"UPDATE {table} SET station=? WHERE station IS NULL OR station=''", (first,))
        # Operator list seeded from the config on the first init (idempotent).
        for cs in _seed_operators():
            c.execute(
                "INSERT OR IGNORE INTO operators(callsign, name, active, created_at) "
                "VALUES (?, '', 1, ?)",
                (cs, int(time.time())),
            )
    _schema_ready.add(str(DB_PATH))


# ── Validation / time ──────────────────────────────────────────────────────


def valid_callsign(value: str) -> bool:
    return bool(_RE_CALLSIGN.match((value or "").strip().upper()))


def valid_locator(value: str) -> bool:
    return not value or bool(_RE_LOCATOR.match(value.strip().upper()))


def paris_local_to_utc_iso(value: str) -> str | None:
    """'YYYY-MM-DDTHH:MM' entered in Paris time → ISO UTC minute.

    Returns None if the format is invalid.
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
    """(qso_date 'YYYYMMDD', time_on 'HHMM') for the current instant in UTC."""
    now = datetime.now(UTC)
    return now.strftime("%Y%m%d"), now.strftime("%H%M")


# ── Local / UTC display ────────────────────────────────────────────────────
# Storage is ALWAYS in UTC. These helpers only drive display and input.
# The "mode" is "utc", "local" (station time zone, config.yml) or directly
# an IANA time zone — the visitor's, as announced by their browser
# ("Europe/Brussels", "America/New_York"…): a Canadian hunter reads the
# slots in their own time without setting anything.

TZ_MODES = ("local", "utc")


def station_tz() -> ZoneInfo:
    """Station time zone (config.yml: site.timezone), Paris by default."""
    name = str(site_config().get("timezone") or "").strip()
    return _zone(name) or PARIS


def _zone(name: str) -> ZoneInfo | None:
    """ZoneInfo for an IANA name, None if unknown (in-memory cache)."""
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
    """Usable IANA time zone name? (what the browser returns)"""
    return _zone(name) is not None


def tzinfo_for(mode: str) -> ZoneInfo | timezone:
    if mode == "utc":
        return UTC
    return _zone(mode) or station_tz()


def tz_label(mode: str) -> str:
    """Short time zone label: "UTC", "Paris", "New York"."""
    if mode == "utc":
        return "UTC"
    name = mode if _zone(mode) else str(site_config().get("timezone") or "Europe/Paris")
    return name.split("/")[-1].replace("_", " ")


def disp(utc_iso: str, mode: str = "local") -> datetime | None:
    """ISO UTC 'YYYY-MM-DDTHH:MM' → aware datetime in the display time zone."""
    try:
        naive = datetime.strptime((utc_iso or "").strip()[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, AttributeError):
        return None
    return naive.replace(tzinfo=UTC).astimezone(tzinfo_for(mode))


def contact_disp(qso_date: str, time_on: str, mode: str = "local") -> datetime | None:
    """(qso_date 'YYYYMMDD', time_on 'HHMM' in UTC) → aware display datetime."""
    try:
        combo = f"{(qso_date or '').strip()}{(time_on or '').strip().ljust(4, '0')[:4]}"
        naive = datetime.strptime(combo[:12], "%Y%m%d%H%M")
    except (ValueError, AttributeError):
        return None
    return naive.replace(tzinfo=UTC).astimezone(tzinfo_for(mode))


def input_to_utc_iso(value: str, mode: str = "local") -> str | None:
    """'datetime-local' input interpreted in time zone `mode` → ISO UTC minute."""
    if not value:
        return None
    try:
        naive = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=tzinfo_for(mode)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def now_input(mode: str = "local") -> str:
    """Current instant in the format of an <input type=datetime-local> in `mode`."""
    return datetime.now(tzinfo_for(mode)).strftime("%Y-%m-%dT%H:%M")


def parts_to_utc_iso(qso_date: str, time_on: str) -> str | None:
    """(qso_date "YYYYMMDD", time_on "HHMM") → "YYYY-MM-DDTHH:MM" UTC."""
    minutes = _utc_minutes(qso_date, time_on)
    return _minutes_to_iso(minutes) if minutes is not None else None


def utc_iso_to_parts(utc_iso: str) -> tuple[str, str] | None:
    """ISO UTC 'YYYY-MM-DDTHH:MM' → (qso_date 'YYYYMMDD', time_on 'HHMM')."""
    try:
        dt = datetime.strptime((utc_iso or "")[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return None
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M")


# ── Operators ──────────────────────────────────────────────────────────────


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
    """Operators who ACTUALLY use the callsign: present in a slot (past /
    current / future) or in a logged QSO. Excludes inactive registered ones."""
    init_db()
    with conn() as c:
        st = (callsign(),)
        ops = {r[0] for r in c.execute("SELECT DISTINCT operator_call FROM slots WHERE station=?", st)}
        ops |= {r[0] for r in c.execute("SELECT DISTINCT operator_call FROM contacts WHERE station=?", st)}
    return sorted(o for o in ops if o)


# ── Slots ──────────────────────────────────────────────────────────────────


def slot_conflicts(start_utc: str, end_utc: str, band: str, exclude_id: int | None = None) -> list[dict[str, Any]]:
    """Existing slots (current callsign) overlapping [start, end[ on the same band."""
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
    """Slots of a callsign (default: the current one)."""
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
    """Ids of the slots that overlap another one on the SAME band."""
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
    """ALL current slots (start <= now < end) — there may be several."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    return [s for s in list_slots(station=station) if s["start_utc"] <= now < s["end_utc"]]


def future_slots(station: str | None = None) -> list[dict[str, Any]]:
    """STRICTLY upcoming slots (start > now) — excludes current ones."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    return [s for s in list_slots(station=station) if s["start_utc"] > now]


def past_slots(station: str | None = None) -> list[dict[str, Any]]:
    """Finished activations (end <= now), most recent first."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    done = [s for s in list_slots(station=station) if s["end_utc"] <= now]
    return sorted(done, key=lambda s: s["start_utc"], reverse=True)


# UTC time of a QSO (qso_date "YYYYMMDD" + time_on "HHMM") in the slot format
# ("YYYY-MM-DDTHH:MM"), so the two can be compared in SQL.
_SQL_QSO_UTC = (
    "substr(c.qso_date,1,4) || '-' || substr(c.qso_date,5,2) || '-' || substr(c.qso_date,7,2)"
    " || 'T' || substr(c.time_on,1,2) || ':' || substr(c.time_on,3,2)"
)


def slot_qso_counts(station: str | None = None) -> dict[int, int]:
    """QSOs logged per slot: same operator, same band, same mode, from the start
    to the end **inclusive**. Returns {slot id: number of QSOs}.

    QSOs are timestamped to the minute: a contact noted at 19:15 happened
    during minute 19:15, so it belongs to the slot ending at 19:15 (it is
    often the last QSO of the satellite pass, the one that closes the
    session). If two slots of the same operator touch at that very minute,
    the QSO is counted in the more recent one — the one that just started —
    and never twice.
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


# ── Slots inferred from the log ────────────────────────────────────────────
# An operator who forgets to book, or who runs past the planned time, loses
# nothing: the recorded QSOs are authoritative. QSOs from the same operator on
# the same band and mode are grouped as long as they are less than
# SESSION_GAP_MIN apart, then the missing slot is created or the existing one
# is stretched (never shortened: the intent of the schedule is kept).

SESSION_GAP_MIN = 30          # beyond this, it is another operating session
SLOT_ATTACH_MIN = 60          # session attached to a nearby slot (overrun)
SLOT_ROUNDING_MIN = 15        # slots aligned on the quarter hour


def auto_slots() -> bool:
    """Create and adjust slots from the log (setting, enabled by default)."""
    return get_flag("auto_slots", True)


# Slot tiles on the public page: 0 = all (default). A number limits
# both the "Prochaines activations" (upcoming) AND the "Activations passées" (past).
PUBLIC_SLOTS_MAX = 200            # safety cap: beyond this the page becomes unreadable


def public_slots_max() -> int:
    """Number of slot tiles shown publicly (0 = all)."""
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
    """Operating sessions read from the log: (operator, band, mode, start, end)."""
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
    """Forbid logging on a band/mode booked by another operator (setting,
    enabled by default): two stations cannot transmit at the same time under
    the same callsign, on the same band and the same mode."""
    return get_flag("slot_lock", True)


def blocking_slot(operator_call: str, band: str, mode: str, when_utc: str | None = None,
                  station: str | None = None) -> dict[str, Any] | None:
    """Slot of ANOTHER operator covering this band, this mode and this instant."""
    cs = (operator_call or "").strip().upper()
    moment = when_utc or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    for slot in list_slots(station=station):
        if (slot["operator_call"] != cs and slot["band"] == (band or "").upper()
                and slot["mode"] == (mode or "").upper()
                and slot["start_utc"] <= moment < slot["end_utc"]):
            return slot
    return None


def reconcile_slots_from_log(station: str | None = None, force: bool = False) -> dict[str, int]:
    """Create forgotten slots and stretch those that overran. {created, stretched}.

    ``force``: the admin asks for it from the Settings, so it runs even if
    automatic catch-up is unchecked ("Mettre à jour maintenant" / Update now button).
    """
    if not (force or auto_slots()):
        return {"created": 0, "extended": 0}
    st = _st(station)
    created = extended = 0
    slots = list_slots(station=st)
    for session in log_sessions(st):
        start = _minutes_to_iso(_floor_to(session["first"], SLOT_ROUNDING_MIN))
        end = _minutes_to_iso(_ceil_to(session["last"] + 1, SLOT_ROUNDING_MIN))
        # The slot is attached if it overlaps the session or comes within
        # SLOT_ATTACH_MIN of it: operating an hour past the planned time
        # extends the booked slot instead of creating another one.
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


# ── Contacts (QSOs) ────────────────────────────────────────────────────────


def is_dupe(call: str, band: str, mode: str) -> bool:
    init_db()
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM contacts WHERE station=? AND call=? AND band=? AND mode=? LIMIT 1",
            (callsign(), (call or "").strip().upper(), band.upper(), mode.upper()),
        ).fetchone()
    return row is not None


def worked_before(call: str, station: str | None = None) -> dict[str, Any]:
    """Station already worked? (shown while logging)

    For the current callsign: number of QSOs, band/mode pairs already done and
    date of the last contact. Also reports QSOs made under the club's OTHER
    special callsigns, which are not duplicates.
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
    # The log is authoritative: forgotten slot created, overrun slot stretched.
    reconcile_slots_from_log(station)
    maybe_backup()
    return new_id


# Editable fields of a contact (editing).
_EDITABLE = (
    "call", "band", "mode", "qso_date", "time_on", "freq_mhz",
    "rst_sent", "rst_rcvd", "gridsquare", "sat_name", "comment", "operator_call",
)


def update_contact(contact_id: int, **fields: Any) -> None:
    """Update the given fields of an existing QSO (with validation)."""
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
    """QSOs of a callsign (default: the current one), most recent first."""
    init_db()
    sql = "SELECT * FROM contacts WHERE station=? ORDER BY qso_date DESC, time_on DESC, id DESC"
    params: list[Any] = [_st(station)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def contacts_for_call(call: str, station: str | None = None) -> dict[str, Any]:
    """Public lookup: a callsign's QSOs with the activation + contest score.

    Returns the contact details and the counters used by the contest
    (total, number of bands, of modes, and of distinct band+mode pairs —
    the "activations" metric of a special station).
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
    """QSOs whose id is in ``ids`` (export of a selection), chronological order."""
    wanted = sorted({int(i) for i in ids})
    out: list[dict[str, Any]] = []
    init_db()
    with conn() as c:
        for start in range(0, len(wanted), 500):  # SQLite variable limit
            chunk = wanted[start:start + 500]
            marks = ",".join("?" * len(chunk))
            out += [dict(r) for r in c.execute(f"SELECT * FROM contacts WHERE id IN ({marks})", chunk)]
    return sorted(out, key=lambda q: (q["qso_date"], q["time_on"], q["id"]))


def delete_contact(contact_id: int) -> None:
    init_db()
    with conn() as c:
        c.execute("DELETE FROM contacts WHERE id=?", (int(contact_id),))
    maybe_backup()


# ── Statistics ─────────────────────────────────────────────────────────────


def stats(station: str | None = None) -> dict[str, Any]:
    """Counters of a callsign (default: the current one)."""
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


# ── Operating rate (log gauges) ────────────────────────────────────────────
# "How many am I making per hour?": the counter that makes you want to keep going.
# Two windows: the last hour (underlying trend) and the last 10 minutes
# (the current pile-up), each compared with the previous window.

RATE_FULL_SCALE = 60        # QSO/h matching a full gauge
RATE_LEVELS = (             # thresholds in QSO/h → displayed label
    (0, N_("station calme")),
    (6, N_("ça démarre")),
    (18, N_("bon rythme")),
    (36, N_("ça chauffe")),
    (60, N_("pile-up !")),
)


def _count_between(c: sqlite3.Connection, station: str, operator: str,
                   start: datetime, end: datetime) -> int:
    """QSOs recorded in [start, end[ (UTC bounds)."""
    sql = ("SELECT COUNT(*) FROM contacts WHERE station=? "
           "AND (qso_date || substr(time_on, 1, 4)) >= ? "
           "AND (qso_date || substr(time_on, 1, 4)) < ?")
    params: list[Any] = [station, start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M")]
    if operator:
        sql += " AND operator_call = ?"
        params.append(operator)
    return int(c.execute(sql, params).fetchone()[0])


def _rate_level(per_hour: float) -> str:
    """Rate label (translated at display time)."""
    label = RATE_LEVELS[0][1]
    for threshold, text in RATE_LEVELS:
        if per_hour >= threshold:
            label = text
    return label


def qso_rate(operator: str = "", station: str | None = None) -> dict[str, Any]:
    """Operating rate of an operator (or of the station if ``operator`` is empty).

    Returns, for the last hour and for the last 10 minutes, the number of
    QSOs, the rate scaled to one hour, the change from the previous period
    and the gauge fill (0 to 100).
    """
    st = _st(station)
    op = (operator or "").strip().upper()
    now = datetime.now(UTC)
    init_db()
    windows = {}
    with conn() as c:
        for name, minutes in (("hour", 60), ("ten", 10)):
            # Upper bound at the next minute: the QSO just recorded
            # (same minute as "now") must count right away.
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
    """DXCC entities worked, the most recently worked first.

    The flag code comes from the callsign prefix (no network needed, the
    thumbnail is served by the application); the country name comes from the
    QRZ callbook when known, otherwise from the prefix table. Callsigns whose
    entity is unknown are skipped.
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
        # A name from QRZ is more accurate than the one from the prefix table.
        qrz_name = known.get(row["call"], "")
        if qrz_name and not item["from_qrz"]:
            item["name"], item["from_qrz"] = qrz_name, True
    out = sorted(seen.values(), key=lambda e: e["last"], reverse=True)
    return out[:limit] if limit else out


# ── QRZ callbook (name / locator / DXCC of worked callsigns) ───────────────
# Permanent cache of QRZ XML lookups: each callsign is queried only once
# (when logged, or by the background task that slowly goes over callsigns
# not yet known). Only callsign, locator and DXCC are published; first and
# last name stay in the operators' area.

CALLBOOK_RETRY_NOTFOUND = 24 * 3600  # unknown to QRZ: retry the next day
CALLBOOK_RETRY_ERROR = 15 * 60       # network / session error: retry sooner

logger = logging.getLogger(__name__)


# QRZ account: by default the one from config.yml (``qrz`` section); the admin may
# enter another one in the Settings (e.g. the radio club's). It is kept apart,
# readable by the service only (0600), never displayed nor copied into the
# downloadable backups.
QRZ_ACCOUNT_FILE = ROOT / "var" / "activation_qrz.json"
_RE_QRZ_USER = re.compile(r"^[A-Za-z0-9_.@/-]{3,40}$")
_qrz_own: QrzXmlClient | None = None
_qrz_own_lock = threading.Lock()


def _own_qrz_account() -> dict[str, str]:
    """Account entered in the Settings ({} if none)."""
    try:
        data = json.loads(QRZ_ACCOUNT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    user, pwd = data.get("username"), data.get("password")
    if isinstance(user, str) and isinstance(pwd, str) and user and pwd:
        return {"username": user, "password": pwd}
    return {}


def qrz_account() -> dict[str, str]:
    """QRZ account in use: username and source (``settings``, ``config`` or "")."""
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
    """Try to log in to QRZ: see ``QrzXmlClient.check_login``."""
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
    """Remove the account from the Settings: back to the config.yml one (if any)."""
    QRZ_ACCOUNT_FILE.unlink(missing_ok=True)


def own_qrz_password(username: str) -> str:
    """Password already saved for this username (field left empty = unchanged)."""
    own = _own_qrz_account()
    return own["password"] if own and own["username"].lower() == username.strip().lower() else ""


def qrz_client() -> Any:
    """QRZ XML client: Settings account, else the site's one (None if none)."""
    global _qrz_own
    own = _own_qrz_account()
    if not own:
        return get_shared_client()
    with _qrz_own_lock:
        if _qrz_own is None or (_qrz_own.username, _qrz_own.password) != (own["username"], own["password"]):
            _qrz_own = QrzXmlClient(own["username"], own["password"])
        return _qrz_own


def locator_center(grid: str) -> tuple[float, float] | None:
    """Center of a 4, 6 or 8 character locator → (lat, lon), None if invalid."""
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
    """Great-circle distance (km) between the centers of two locators."""
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
    """Store the result of a lookup (``rec`` = XmlLookup if status ok)."""
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
                int(time.time()),          # photo looked up: no need to come back
                int(time.time()),
            ),
        )


def _photo_url(url: str) -> str:
    """URL of the QRZ photo, kept only if it is safe.

    Only an https URL to qrz.com is accepted: the thumbnail is shown in the
    operators' area, no way we load just any domain there."""
    clean = (url or "").strip()
    if not clean.lower().startswith("https://"):
        return ""
    host = clean.split("/", 3)[2].lower().split(":")[0]
    ok = host == "qrz.com" or host.endswith(".qrz.com")
    return clean[:300] if ok and len(clean) < 300 else ""


def _callbook_fresh(row: dict[str, Any] | None) -> bool:
    """Does the record spare us from querying QRZ again?"""
    if row is None:
        return False
    if row["status"] == "ok":
        # Record older than the thumbnail feature: read it again ONCE for the photo.
        return bool(row.get("image_at"))
    ttl = CALLBOOK_RETRY_NOTFOUND if row["status"] == "notfound" else CALLBOOK_RETRY_ERROR
    return time.time() - (row["fetched_at"] or 0) < ttl


def qrz_lookup(call: str, client: Any) -> dict[str, Any] | None:
    """Callbook record of a callsign: SQLite cache, else QRZ XML.

    Returns the ``callbook`` row if the callsign is known to QRZ, None otherwise
    (unknown, error, or no client). A portable (``DL1ABC/P``) not found as
    is gets looked up under its base callsign.
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
    """Worked callsigns without a valid callbook record (never-seen ones first).

    Includes records from before the QRZ thumbnail: they are correct but
    never had a photo, the background task goes over them once."""
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
    """Query QRZ for ONE pending callsign. Returns the resulting status
    (``ok`` / ``notfound`` / ``error``), or None if there was nothing to do."""
    if client is None:
        return None
    pending = pending_callbook_calls(1)
    if not pending:
        return None
    qrz_lookup(pending[0], client)
    row = callbook_get(pending[0])
    return row["status"] if row else None


def callbook_progress() -> dict[str, int]:
    """Progress of the QRZ enrichment (shown in the Settings)."""
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


# Background task: one callsign every `qrz_interval_seconds` (default 10 s)
# so as not to load QRZ; long pause when there is nothing to do or when
# QRZ misbehaves. The service runs a single uvicorn worker → a single thread.

_enricher_stop = threading.Event()
_enricher_thread: threading.Thread | None = None


def _enricher_loop(interval: float) -> None:
    while not _enricher_stop.is_set():
        delay = 60.0                  # nothing pending: check again in 1 min
        try:
            status = enrich_one(qrz_client())
            if status == "error":
                delay = 300.0         # QRZ down / session refused: back off
            elif status is not None:
                delay = interval
        except Exception:  # noqa: BLE001 — the background task must never die
            logger.exception("enrichissement QRZ : erreur inattendue")
            delay = 300.0
        _enricher_stop.wait(delay)


def start_enricher() -> None:
    """Start the QRZ enrichment as a background task (idempotent).

    Can be disabled with ``activation.qrz_enrich: false`` in config.yml.
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


# ── Public board: map, DXCC, ranking ───────────────────────────────────────


GRID_MISMATCH_KM = 500  # entered more than 500 km from QRZ → entry deemed wrong


def bearing_deg(grid_from: str, grid_to: str) -> float | None:
    """True azimuth (0 = north, 90 = east) between the centers of two locators.

    Used by the compass on the log page: at a glance, the operator knows
    where to turn the antenna."""
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
    """Locator chosen for each worked callsign ("" if unknown).

    The most recent one entered in a QSO, else the QRZ one. The QRZ one wins
    when the entered locator has only 4 characters of the same square, ends
    with "AA" (default value some logging programs make up from the prefix)
    or lies more than ``GRID_MISMATCH_KM`` away from the QRZ one (e.g. the
    previous station's locator left in the software).
    Used for the map and the distance points; the log is not modified.
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
            (len(grid) == 4 and qrz.startswith(grid))    # QRZ refines the same square
            or (len(grid) == 6 and grid.endswith("AA"))  # "xxxxAA": filled in by software
            or (distance_km(grid, qrz) or 0) > GRID_MISMATCH_KM  # obviously wrong entry
        ):
            grid = qrz
        out[call] = grid if locator_center(grid) else ""
    return out


def map_data(station: str | None = None) -> dict[str, Any]:
    """Worked stations for the public map: one point per locator, band and
    mode.

    Locator: see ``call_grids``. Position = CENTER of the locator (never QRZ's
    precise coordinates) and no personal data: callsign + locator only.
    Points in the same square are spread apart on display
    (see static/js/activation-map.js) so they all stay visible.
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
    """DXCC entity name inferred from the prefix ("" if the prefix is unknown)."""
    return dxcc_flags.entity_for_call(call)[1]


def satellite_stats(station: str | None = None) -> dict[str, Any]:
    """QSOs made via satellite, broken down by satellite.

    ``by_sat``: [{"sat", "n", "stations"}] from most to least worked;
    ``total``: number of satellite QSOs; ``share``: their share of the
    log. The name is the one entered in the log (a typo such as "F0-29"
    therefore shows up as is — that is how it gets spotted).
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
    """Activity rhythm: QSOs per day and per UTC hour.

    ``by_day`` (chronological), ``by_hour`` (24 values, 0 h → 23 h UTC),
    the best day, the best clock hour (all days combined), the best single
    one-hour slot, the number of hours the station was active and the
    average number of QSOs over those hours.
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


# ── "Run" periods (pile-up) ────────────────────────────────────────────────
# A run is when things take off: QSOs follow each other with no dead time.
# We pick the contacts whose LOCAL rate (sliding window) exceeds a threshold,
# then stitch together those that follow each other. The result tells when the
# station ran best, and how fast.

RUN_WINDOW_MIN = 10        # sliding observation window
RUN_MIN_RATE = 30          # QSO/h: below this, it is not a run
RUN_MIN_QSOS = 5           # a burst of two contacts is not a run


def _qso_minutes(station: str) -> list[int]:
    """QSO instants, in minutes since the epoch, in order."""
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
    """Best moments of the activation: periods when the rate takes off.

    Returns ``periods`` (the ``top`` best ones, by decreasing rate), each with
    its start, end, duration, number of QSOs and rate, plus ``best`` (the
    best one) and the total number of QSOs made during runs.
    """
    minutes = _qso_minutes(_st(station))
    if not minutes:
        return {"periods": [], "best": None, "total": 0, "share": 0.0}
    hot: list[bool] = []
    half = RUN_WINDOW_MIN / 2.0
    low = high = 0
    for at in minutes:
        # Window CENTERED on the QSO: with a forward-looking-only window,
        # the last contacts of a burst looked calm.
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
    """Summary of a run of close QSOs: duration, rate, start and end (UTC)."""
    start, end = minutes[0], minutes[-1]
    # A single QSO lasts at least the observation window: otherwise the rate
    # of a burst of three contacts in the same minute would be infinite.
    span = max(end - start, RUN_WINDOW_MIN)
    return {
        "start": datetime.fromtimestamp(start * 60, UTC).strftime("%Y-%m-%dT%H:%M"),
        "end": datetime.fromtimestamp(end * 60, UTC).strftime("%Y-%m-%dT%H:%M"),
        "minutes": end - start,
        "qsos": len(minutes),
        "rate": round(len(minutes) * 60.0 / span, 1),
    }


def dxcc_table(station: str | None = None) -> dict[str, Any]:
    """DXCC entities worked: stations and QSOs per entity.

    The entity comes from the QRZ callbook when it knows it, else from the
    **callsign prefix**: without a QRZ account (or before the callbook is
    filled), the table is still right for the vast majority of stations. Only
    callsigns whose prefix is unknown remain "not yet identified".
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
        # Grouping by entity: the code merges name variants
        # ("Germany" on the prefix side, "Fed. Rep. of Germany" on the QRZ side) AND the
        # two sources — a prefix missing from the table must not create a second
        # entity, without a flag, next to the same known entity.
        key, code, prefix_name = dxcc_flags.entity_key(row["call"], qrz_name)
        if not key:
            unidentified += 1
            continue
        item = groups.setdefault(key, {"dxcc": row["dxcc"], "code": code,
                                       "dxcc_name": qrz_name or prefix_name,
                                       "stations": 0, "qsos": 0, "from_qrz": bool(qrz_name)})
        item["stations"] += 1
        item["qsos"] += int(row["qsos"])
        if qrz_name and not item["from_qrz"]:      # the official QRZ name wins
            item["dxcc_name"], item["dxcc"], item["from_qrz"] = qrz_name, row["dxcc"], True
    entities = sorted(groups.values(),
                      key=lambda e: (-e["stations"], -e["qsos"], e["dxcc_name"]))
    return {"entities": entities, "count": len(entities), "unidentified": unidentified}


# ── Hunter certificates (configurable) ─────────────────────────────────────
# A hunter who finds their QSOs on the public page can leave with their
# certificate as a PDF. Disabled until the admin wants it: the artwork
# bears the club's name, better have them proofread it first.

DEFAULT_CERTIFICATE: dict[str, Any] = {
    "enabled": False,    # "Certificat" button on the public page
    "names": False,      # print the hunter's name (otherwise: their callsign only)
    "ranking": True,     # medal with the ranking position
    "mention": "",       # small free-text line at the bottom of the page
    "max_qso": 10,       # contacts listed on the main page
    "appendix": True,    # appendix page(s) with the full log
    "flag": "auto",      # country flag: "" (none), "auto" (from the callsign) or a code
    "border": False,     # thin border around the page
    "border_colors": ["#0055A4", "#FFFFFF", "#EF3340"],
    "emblem": True,      # pylon + banner emblem under the radio set
    "emblem_text": "",   # banner text (club name); empty → "HAM RADIO"
    "ham_symbol": False, # international amateur radio symbol (diamond)
    "qr_url": "",        # QR code URL (left column); empty → no QR
}
_RE_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
CERTIFICATE_MENTION_MAX = 160
CERTIFICATE_EMBLEM_MAX = 40     # beyond this, the banner would become unreadable
CERTIFICATE_QR_MAX = 200        # a denser QR could no longer be read at 2 cm
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
        # "annexe": key name before 1.40, still read.
        "appendix": bool(saved.get("appendix", saved.get("annexe", d["appendix"]))),
        "flag": _clean_flag(saved.get("flag", d["flag"])),
        "border": bool(saved.get("border", d["border"])),
        "border_colors": _clean_colors(saved.get("border_colors"), d["border_colors"]),
        "emblem": bool(saved.get("emblem", d["emblem"])),
        "emblem_text": str(saved.get("emblem_text") or "")[:CERTIFICATE_EMBLEM_MAX],
        "ham_symbol": bool(saved.get("ham_symbol", d["ham_symbol"])),
        "qr_url": _clean_qr_url(saved.get("qr_url")),
    }


def _clean_qr_url(value: Any) -> str:
    """http(s) URL of the QR code; "https://" added if missing, else nothing."""
    url = "".join(str(value or "").split())
    if url and "://" not in url:
        url = "https://" + url
    return url if len(url) <= CERTIFICATE_QR_MAX and _RE_QR_URL.match(url) else ""


def _clean_flag(value: Any) -> str:
    """Either "auto", a known entity code, or nothing."""
    code = str(value or "").strip().upper()
    if code in ("", "AUTO"):
        return code.lower()
    return code if any(code == c for c, _n in dxcc_flags.PREFIXES.values()) else ""


def _clean_colors(value: Any, default: list[str]) -> list[str]:
    """Three #rrggbb colors; any dubious value falls back to the default."""
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
        "appendix": bool(form.get("appendix")),
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
    """Entities available for the certificate flag: (code, name), sorted."""
    seen = {code: name for code, name in dxcc_flags.PREFIXES.values()}
    return sorted(seen.items(), key=lambda item: item[1])


def certificates_on() -> bool:
    return get_certificate_options()["enabled"]


# ── Contacted station's card on the log page (configurable) ────────────────
# While logging: the photo from the QRZ record and a compass showing where
# to turn the antenna. Each is sized in pixels, 0 = not shown.

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


# ── PDF report and club logo (configurable in the Settings) ────────────────

DEFAULT_REPORT: dict[str, Any] = {
    "hours": False,      # "Rythme, heure par heure": left out of the report by default
    "dxcc_all": True,    # all entities with their flag (otherwise: the top ten)
    "hunters": 10,       # number of hunters listed (0 = no leaderboard)
    "sats": True,        # "Satellites": hidden anyway without satellite QSOs
    "one_page": False,   # fit everything on one page, even if lists get cut
    "runs": 3,           # best moments (pile-up) listed; 0 = section removed
}
REPORT_HUNTERS_MAX = 100

LOGO_DIR = ROOT / "var" / "branding"
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg"}


def get_report_options() -> dict[str, Any]:
    """Content of the PDF report (optional sections)."""
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
    """Is the logo shown on the web pages (callsign banner)?"""
    return get_flag("logo_on_pages", True)


def logo_path() -> Path | None:
    """Club logo file (None if there is none)."""
    for ext in LOGO_TYPES:
        candidate = LOGO_DIR / f"logo.{ext}"
        if candidate.is_file():
            return candidate
    return None


def logo_info() -> dict[str, Any] | None:
    """Published logo: path, type, size and date (for the Settings preview)."""
    path = logo_path()
    if path is None:
        return None
    stat = path.stat()
    return {"path": path, "kind": path.suffix.lstrip("."), "bytes": stat.st_size,
            "mtime": int(stat.st_mtime), "media_type": LOGO_TYPES[path.suffix.lstrip(".")]}


def _image_kind(data: bytes) -> str:
    """Returns "png", "jpg" or "": we trust the CONTENT, not the file name."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    return ""


def set_logo(data: bytes) -> str:
    """Save the logo (PNG or JPEG). Returns the type; ValueError otherwise."""
    if not data:
        raise ValueError(_("fichier vide"))
    if len(data) > LOGO_MAX_BYTES:
        raise ValueError(_("image trop lourde (2 Mo maximum)"))
    kind = _image_kind(data)
    if not kind:
        raise ValueError(_("format non reconnu : attendu PNG ou JPEG"))
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    for ext in LOGO_TYPES:          # only one logo at a time
        (LOGO_DIR / f"logo.{ext}").unlink(missing_ok=True)
    (LOGO_DIR / f"logo.{kind}").write_bytes(data)
    return kind


def clear_logo() -> None:
    for ext in LOGO_TYPES:
        (LOGO_DIR / f"logo.{ext}").unlink(missing_ok=True)


# ── Public map style (configurable by the admin in the Settings) ───────────
# One point per locator × band × mode: the COLOR tells the mode, the SHAPE tells
# the band. Both tables are editable; a mode or band missing from the table
# takes the "others" value.

# Order chosen so that two neighboring bands do not look alike: the table
# cycles through the shapes, so beyond 8 bands a shape will be reused.
MAP_SHAPES = ("circle", "diamond", "square", "triangle", "star", "cross", "hexagon", "pentagon")

DEFAULT_MAP_STYLE: dict[str, Any] = {
    "enabled": True,                  # unchecked: all points identical
    "filters": True,                  # band/mode checkboxes under the map
    "mode_colors": {
        "SSB": "#e8543f", "CW": "#f2b134", "FT8": "#2f7fd1", "FT4": "#17a2a2",
        "RTTY": "#8e5bd0", "PSK31": "#d2691e", "FM": "#2fa84f", "AM": "#8a8f98",
        "SSTV": "#e0559c", "DIGI": "#1f6f8b",
    },
    "mode_default": "#5b6b7c",        # modes missing from the table
    "band_shapes": {
        band: MAP_SHAPES[i % len(MAP_SHAPES)] for i, band in enumerate(BANDS)
    },
    "band_default": "circle",         # bands missing from the table
}

_RE_COLOR = re.compile(r"^#[0-9a-f]{6}$")


def _clean_color(value: Any, default: str) -> str:
    color = str(value or "").strip().lower()
    return color if _RE_COLOR.match(color) else default


def _clean_shape(value: Any, default: str) -> str:
    shape = str(value or "").strip().lower()
    return shape if shape in MAP_SHAPES else default


def get_map_style(station: str | None = None) -> dict[str, Any]:
    """Colors (modes) and shapes (bands) of a callsign's map."""
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
    """Save the map style; "reset" goes back to the default values."""
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


# ── Points rule (configurable by the admin in the Settings) ────────────────
# Points of a QSO = points per contact + mode points + distance points
# (each criterion can be enabled). A hunter's score = sum of their QSOs; with
# "one QSO per band×mode", only the best QSO of each pair counts.

DEFAULT_SCORING: dict[str, Any] = {
    "enabled": False,           # points ranking (otherwise: band×mode)
    "unique_band_mode": True,   # band×mode duplicates count 0
    "per_qso_on": True,
    "per_qso": 1,               # points per contact
    "mode_on": True,
    "mode_points": {"CW": 3, "SSB": 2, "FM": 2, "AM": 2, "RTTY": 2, "SSTV": 2,
                    "FT8": 1, "FT4": 1, "PSK31": 1, "DIGI": 1},
    "mode_default": 1,          # unlisted modes
    "distance_on": True,
    "km_per_point": 500,        # 1 point per full N km
    "distance_max": 0,          # cap on distance points (0 = none)
}


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def get_scoring(station: str | None = None) -> dict[str, Any]:
    """Points rule of a callsign (default: current), completed with the defaults."""
    saved = (load_settings().get("scoring_by_station") or {}).get(_st(station)) or {}
    rule = {**DEFAULT_SCORING, **{k: v for k, v in saved.items() if k in DEFAULT_SCORING}}
    rule["mode_points"] = {**DEFAULT_SCORING["mode_points"], **(saved.get("mode_points") or {})}
    return rule


def set_scoring(form: dict[str, Any], station: str | None = None) -> dict[str, Any]:
    """Save the points rule of a callsign (default: current), values clamped."""
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
    """Points of a QSO under the rule (unknown distance → 0 distance points)."""
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
    """Points rule in one sentence (under the ranking, in the Settings)."""
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
    # Points (all 0 if the rule is disabled), then distinct band×mode pairs,
    # QSOs, and finally the first one to have reached that total.
    return (-h["points"], -h["band_modes"], -h["qsos"], h["last"], h["call"])


def hunters_ranking(limit: int | None = 50, station: str | None = None) -> list[dict[str, Any]]:
    """Ranking of hunters (stations that worked the callsign).

    By points if the rule is enabled (``get_scoring``), otherwise by distinct
    band×mode pairs. Distance: from the station's locator
    (``my_gridsquare``) to the one chosen for the hunter (``call_grids``).
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
    """Emit a combined ADIF (STATION_CALLSIGN = activation callsign)."""
    if contacts is None:
        contacts = list_contacts()
    station = callsign()
    grids: dict[str, str] = {}  # locator of each callsign encountered
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


# ── ADIF import: preview, then import of the checked QSOs ──────────────────
# 1. analyze_adif() gives a status to each QSO in the file; 2. the operator
# confirms the checked QSOs (import_rows). In between, the file waits
# in var/cache under a random token (purged after one hour).

IMPORT_TMP_DIR = ROOT / "var" / "cache" / "activation-import"
IMPORT_TMP_TTL = 3600
IMPORT_TIME_TOLERANCE_MIN = 10  # same QSO within ±10 min (software clocks)

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
    """(YYYYMMDD, HHMM[SS]) in UTC → minutes since the epoch, None if invalid."""
    try:
        dt = datetime.strptime(f"{qso_date}{(time_on or '')[:4]}", "%Y%m%d%H%M")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=UTC).timestamp()) // 60


def _adif_mode(r: dict[str, str]) -> str:
    mode = (r.get("mode") or "").strip().upper()
    submode = (r.get("submode") or "").strip().upper()
    if mode in ("USB", "LSB"):                   # non-standard but common
        return "SSB"
    if submode and mode in ("", "MFSK", "PSK"):  # ADIF 3: FT4 = MFSK / FT4
        return submode
    return mode


def analyze_adif(
    text: str, operator_call: str = "", prefer_file_operator: bool = False,
) -> list[dict[str, Any]]:
    """Analyze an ADIF file before import: one dict per QSO, with ``status``.

    All QSOs are attached to ``operator_call``; with ``prefer_file_operator``,
    the ADIF OPERATOR field takes precedence when valid (unless it equals
    the activation callsign, which some programs put there).
    Statuses: see ``IMPORT_STATUS``. Two QSOs are "the same" if they have the
    same callsign, band and mode within ±``IMPORT_TIME_TOLERANCE_MIN`` minutes.
    """
    from app.wavelog_client import parse_adif  # local import: avoids a cycle

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
    """Import the analyzed QSOs whose index is checked (never the invalid ones)."""
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
    """Direct import, without preview: takes new QSOs or ones already worked
    on the band/mode; skips those already in the log, duplicated or invalid."""
    rows = analyze_adif(text, operator_call, prefer_file_operator)
    return import_rows(rows, {r["idx"] for r in rows if r["status"] in IMPORT_DEFAULT_CHECKED})


def stash_import(text: str) -> str:
    """Keep an ADIF file for the duration of the preview; returns its token."""
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


# ── Backups ────────────────────────────────────────────────────────────────
# Consistent snapshots of the database (SQLite backup API) + copy of the
# operators' password, in var/backups/, with rotation. No real data is ever
# deleted: the rotation touches ONLY the snapshots folder.


def _rotate_backups() -> None:
    for pattern in ("activation-*.sqlite", "password-*.txt", "settings-*.json"):
        files = sorted(BACKUP_DIR.glob(pattern))
        for old in files[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)


def backup_now() -> Path:
    """Write a timestamped snapshot of the database (+ password) and return it."""
    init_db()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"activation-{stamp}.sqlite"
    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)  # consistent snapshot even during a write
        finally:
            dst.close()
    finally:
        src.close()
    # The operator password lives in a separate file: include it.
    if OP_PASSWORD_FILE.is_file():
        pw_copy = BACKUP_DIR / f"password-{stamp}.txt"
        pw_copy.write_text(OP_PASSWORD_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        pw_copy.chmod(0o600)
    # The settings (flags) too.
    if SETTINGS_FILE.is_file():
        (BACKUP_DIR / f"settings-{stamp}.json").write_text(
            SETTINGS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
        )
    _rotate_backups()
    global _last_backup_ts
    _last_backup_ts = time.time()
    return dest


def maybe_backup() -> None:
    """Throttled backup, called after each write (best-effort)."""
    global _last_backup_ts
    if time.time() - _last_backup_ts < _BACKUP_MIN_INTERVAL:
        return
    try:
        backup_now()
    except Exception:  # noqa: BLE001 — a failing backup must not break anything
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
