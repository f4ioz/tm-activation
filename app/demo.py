"""Demo mode: a public sandbox with fictitious data, reset on a timer.

Enabled by the ``demo`` section of config.yml (``install.sh --demo``):

- demo accounts, all sharing one published password: two operators, one
  admin, one superadmin (``ACCOUNTS``);
- a log filled with representative fictitious QSOs, slots and hunters;
- a "DEMO" banner on every page, with the countdown to the next reset;
- the whole data set rebuilt every ``reset_hours`` (24 h by default);
- restrictions: what could harm the server or other visitors is refused
  (``guard`` + ``install_limits``).

Safety: the reset OVERWRITES the activation database. It only ever runs when
config.yml says ``demo.enabled: true`` AND the ``var/DEMO_INSTANCE`` marker
exists — a file only install.sh creates, on a fresh demo installation. A real
installation never has it, so a mistake in config.yml cannot wipe a real log.

The seed is generated in a separate process (``python -m app.demo --seed DIR``)
pointed at a scratch folder, then copied over the live database with the
SQLite backup API: the running server never swaps its own paths.
"""

from __future__ import annotations

import json
import logging
import os
import random
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from app import activation, dxcc_flags, i18n, security, visits
from app.config import activation_config, load_config
from app.i18n import _

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
VAR_DIR = ROOT / "var"
MARKER_FILE = VAR_DIR / "DEMO_INSTANCE"      # created by install.sh --demo only
STATE_FILE = VAR_DIR / "demo_state.json"     # time of the last reset
WORK_DIR = VAR_DIR / "demo-seed"             # scratch folder of the seed process

DEFAULT_PASSWORD = "Demo-73!"
DEFAULT_HOURS = 24
HOURS_MIN, HOURS_MAX = 1, 168
RETRY_AFTER_FAILURE = 600                    # a failed reset is retried 10 min later
SEED_TIMEOUT = 300

# Demo accounts: (callsign, role). Protected: nobody can change their password,
# rights or state — otherwise the first visitor could lock the others out.
ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("M0DEMO1", "operator"),
    ("TM0DEMO2", "operator"),
    ("TM0ADM11", "admin"),
    ("TM0SADM1", "superadmin"),
)
PROTECTED = frozenset(call for call, _role in ACCOUNTS)

# Caps: a visitor cannot fill the disk or the database.
MAX_BODY = 256 * 1024          # bytes per request (an ADIF of ~1000 QSOs)
MAX_DB_BYTES = 40 * 1024 * 1024
MAX_CONTACTS = 3000
MAX_SLOTS = 400
MAX_OPERATORS = 60
MAX_STATIONS = 6
POSTS_PER_MINUTE = 40          # per IP
HEAVY_PER_MINUTE = 8           # PDF / exports per IP

# Refused whatever the role: credentials, shared secrets, uploads, backups
# (a backup holds the password hashes of the visitors' accounts).
BLOCKED_POST = frozenset({
    "/activation/settings/password",
    "/activation/settings/auth",
    "/activation/settings/qrz",
    "/activation/settings/backup",
    "/activation/settings/logo",
    "/activation/settings/logo/show",
    "/activation/settings/logo/delete",
})
BLOCKED_GET = frozenset({"/activation/settings/backup.sqlite"})
HEAVY_SUFFIXES = ("/report.pdf", "/certificat", "/export.adi", "/export.csv", "/export-selection.adi")


# ── Configuration ──────────────────────────────────────────────────────────


def demo_config() -> dict[str, Any]:
    return load_config().get("demo") or {}


def enabled() -> bool:
    return bool(demo_config().get("enabled"))


def reset_hours() -> int:
    try:
        hours = int(demo_config().get("reset_hours") or DEFAULT_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    return max(HOURS_MIN, min(HOURS_MAX, hours))


def password() -> str:
    pw = str(demo_config().get("password") or DEFAULT_PASSWORD)
    # Published on every page: it must at least pass the password rule.
    return pw if activation.password_is_strong(pw) else DEFAULT_PASSWORD


def reset_allowed() -> bool:
    """The data may be overwritten: demo enabled AND demo installation marker."""
    return enabled() and MARKER_FILE.is_file()


# ── Reset schedule ─────────────────────────────────────────────────────────


def last_reset() -> float:
    try:
        return float(json.loads(STATE_FILE.read_text(encoding="utf-8"))["last_reset"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _write_state(ts: float) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_reset": ts}), encoding="utf-8")
    tmp.replace(STATE_FILE)


def next_reset() -> float:
    return last_reset() + reset_hours() * 3600


def info() -> dict[str, Any] | None:
    """Banner data (None outside demo mode)."""
    if not enabled():
        return None
    nxt = next_reset() if reset_allowed() else 0.0
    return {
        "hours": reset_hours(),
        "next_reset_iso": (datetime.fromtimestamp(nxt, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                           if nxt else ""),
        "remaining": max(0, int(nxt - time.time())) if nxt else 0,
        "password": password(),
        "accounts": [{"call": call, "role": role} for call, role in ACCOUNTS],
    }


_reset_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None


def reset_now() -> bool:
    """Rebuild the demo data set. Refused (False) outside a demo installation."""
    if not reset_allowed():
        return False
    with _reset_lock:
        try:
            if WORK_DIR.exists():
                shutil.rmtree(WORK_DIR)
            WORK_DIR.mkdir(parents=True)
            proc = subprocess.run(
                [sys.executable, "-m", "app.demo", "--seed", str(WORK_DIR)],
                cwd=ROOT, capture_output=True, text=True, timeout=SEED_TIMEOUT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip()[-800:] or f"exit {proc.returncode}")
            _install_seed(WORK_DIR)
        except Exception:  # noqa: BLE001 — retried later, the site keeps running
            log.exception("demo reset failed")
            _write_state(time.time() - reset_hours() * 3600 + RETRY_AFTER_FAILURE)
            return False
        finally:
            shutil.rmtree(WORK_DIR, ignore_errors=True)
        _write_state(time.time())
        log.info("demo data reset")
        return True


def _install_seed(work: Path) -> None:
    """Copy the generated data set over the live data (demo installation only)."""
    assert reset_allowed()  # noqa: S101 — last safeguard before overwriting
    src = sqlite3.connect(work / "activation.sqlite")
    try:
        dst = sqlite3.connect(activation.DB_PATH)
        try:
            src.backup(dst)   # replaces the whole database, consistent for readers
        finally:
            dst.close()
    finally:
        src.close()
    settings = (work / "activation_settings.json").read_text(encoding="utf-8")
    tmp = activation.SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(settings, encoding="utf-8")
    tmp.replace(activation.SETTINGS_FILE)
    # What visitors may have left behind (normally refused, cleaned anyway).
    for path in (activation.OP_PASSWORD_FILE, activation.QRZ_ACCOUNT_FILE):
        path.unlink(missing_ok=True)
    for logo in activation.LOGO_DIR.glob("logo.*") if activation.LOGO_DIR.is_dir() else ():
        logo.unlink(missing_ok=True)
    shutil.rmtree(activation.IMPORT_TMP_DIR, ignore_errors=True)


def _loop() -> None:
    while not _stop.wait(30):
        if time.time() >= next_reset():
            reset_now()


def start() -> None:
    """At startup: first data set if needed, then the reset timer."""
    global _thread
    if not enabled():
        return
    install_limits()
    if not reset_allowed():
        log.warning("demo mode without %s: data will NOT be reset", MARKER_FILE)
        return
    if not last_reset() or not activation.DB_PATH.is_file():
        reset_now()           # first start: the site must not open empty
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="demo-reset", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()


# ── Restrictions ───────────────────────────────────────────────────────────


def _refuse(message: str) -> None:
    raise ValueError(message)


def _count(table: str) -> int:
    activation.init_db()
    with activation.conn() as c:
        return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 — fixed names


_installed = False


def install_limits() -> None:
    """Wrap the data-layer functions the routes call (defense in depth: the
    middleware already refuses most of these requests)."""
    global _installed
    if _installed:
        return
    _installed = True
    a = activation
    blocked = _("Désactivé en mode démo.")

    def protect(fn):
        def wrapper(call, *args, **kwargs):
            if (call or "").strip().upper() in PROTECTED:
                _refuse(_("Compte de démonstration protégé : il ne peut pas être modifié."))
            return fn(call, *args, **kwargs)
        return wrapper

    for name in ("set_operator_password_for", "clear_operator_password", "set_operator_admin",
                 "set_operator_superadmin", "set_operator_active"):
        setattr(a, name, protect(getattr(a, name)))

    for name in ("set_operator_password", "set_qrz_account", "set_logo"):
        setattr(a, name, lambda *args, **kwargs: _refuse(blocked))
    # QRZ: never on the owner's account, even if config.yml has one.
    a.qrz_client = lambda: None

    orig_login = a.operator_login

    def operator_login(call: str, password_: str) -> str:
        if a.get_operator(call) is None and _count("operators") >= MAX_OPERATORS:
            return "disabled"            # a known code: the login page explains it
        return orig_login(call, password_)
    a.operator_login = operator_login

    orig_add_operator = a.add_operator

    def add_operator(call: str, name: str = "") -> None:
        if a.get_operator(call) is None and _count("operators") >= MAX_OPERATORS:
            _refuse(_("Limite de la démo atteinte."))
        orig_add_operator(call, name)
    a.add_operator = add_operator

    orig_add_slot = a.add_slot

    def add_slot(*args: Any, **kwargs: Any) -> int:
        if _count("slots") >= MAX_SLOTS:
            _refuse(_("Limite de la démo atteinte."))
        return orig_add_slot(*args, **kwargs)
    a.add_slot = add_slot

    orig_create_station = a.create_station

    def create_station(call: str, **fields: Any) -> dict[str, Any]:
        if _count("stations") >= MAX_STATIONS:
            _refuse(_("Limite de la démo atteinte."))
        return orig_create_station(call, **fields)
    a.create_station = create_station

    orig_cert = a.set_certificate_options

    def set_certificate_options(form: dict[str, Any]) -> dict[str, Any]:
        # A QR code sends the hunters who scan a certificate to that URL:
        # it stays the one of the demo.
        return orig_cert({**form, "qr_url": a.get_certificate_options()["qr_url"]})
    a.set_certificate_options = set_certificate_options


def _page(request: Request, status: int, message: str) -> Response:
    i18n.use(i18n.detect(request))
    if request.headers.get("HX-Request"):
        return HTMLResponse(f'<p class="act-alert act-alert-err">{_escape(message)}</p>', status_code=200)
    from app.templating import templates   # late import: templating imports this module
    station = activation.current_station()
    return templates.TemplateResponse(
        request, "demo_blocked.html",
        {"callsign": station["callsign"], "label": station["label"] or station["callsign"],
         "station": station, "message": message},
        status_code=status,
    )


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _write_caps(path: str) -> str | None:
    """Reason to refuse a write that would exceed a cap (None = allowed)."""
    try:
        if activation.DB_PATH.is_file() and activation.DB_PATH.stat().st_size > MAX_DB_BYTES:
            return _("Limite de la démo atteinte.")
    except OSError:
        pass
    if path in ("/activation/contacts", "/activation/import", "/activation/import/confirm") \
            and _count("contacts") >= MAX_CONTACTS:
        return _("Limite de la démo atteinte : {n} QSO. Remise à zéro prochainement.", n=MAX_CONTACTS)
    return None


def guard(request: Request) -> Response | None:
    """Demo restrictions for one request (None = let it through)."""
    if not enabled():
        return None
    path = request.scope.get("path", "")
    method = request.method.upper()
    ip = visits.client_ip(request)
    if method == "GET":
        if path in BLOCKED_GET:
            return _page(request, 403, _("Désactivé en mode démo."))
        if path.endswith(HEAVY_SUFFIXES) and security.rate_limited(f"demo-heavy:{ip}", HEAVY_PER_MINUTE, 60):
            return _page(request, 429, _("Trop de demandes : patientez une minute."))
        return None
    if method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    length = request.headers.get("content-length")
    if length is None or not length.isdigit() or int(length) > MAX_BODY:
        return _page(request, 413, _("Envoi trop volumineux pour la démo ({kb} Ko au plus).", kb=MAX_BODY // 1024))
    if security.rate_limited(f"demo-post:{ip}", POSTS_PER_MINUTE, 60):
        return _page(request, 429, _("Trop de demandes : patientez une minute."))
    if path in BLOCKED_POST:
        return _page(request, 403, _("Désactivé en mode démo."))
    if path.startswith("/activation/settings/operators/"):
        if path.rsplit("/", 1)[-1].strip().upper() in PROTECTED:
            return _page(request, 403, _("Compte de démonstration protégé : il ne peut pas être modifié."))
    if (reason := _write_caps(path)) is not None:
        return _page(request, 403, reason)
    return None


def add_headers(response: Response) -> None:
    """Demo content is written by anyone: never indexed by search engines."""
    if enabled():
        response.headers["X-Robots-Tag"] = "noindex, nofollow"


# ── Fictitious data set ────────────────────────────────────────────────────
# Representative traffic: European hunters in majority, some DX, pile-ups on
# 20 m / 40 m, a satellite pass, an operating session live right now.

# (entity prefixes, base locators) — callsigns are made up at random.
_HUNTERS: tuple[tuple[tuple[str, ...], tuple[str, ...], int], ...] = (
    (("F1", "F4", "F5", "F6", "F8"), ("JN18", "JN03", "IN88", "JN24", "JN12", "IN94", "JN05", "JN26", "JN38", "IN97"), 30),
    (("DL1", "DK2", "DJ5", "DO7", "DL9", "DG3"), ("JO62", "JN58", "JO31", "JO53", "JN48", "JO40"), 14),
    (("G0", "G4", "M0", "M7", "2E0"), ("IO91", "IO83", "JO01", "IO70", "IO92"), 8),
    (("ON4", "ON5", "ON3", "OO7"), ("JO20", "JO10", "JO21", "JO11"), 7),
    (("PA3", "PD0", "PE1", "PA0"), ("JO22", "JO21", "JO32", "JO33"), 5),
    (("IK2", "IZ5", "IW3", "I1"), ("JN45", "JN61", "JN54", "JN71", "JN44"), 6),
    (("EA4", "EA1", "EA5", "EA7"), ("IN80", "IN52", "IM87", "JN11", "IM76"), 5),
    (("OE1", "OE3", "OE5"), ("JN88", "JN77", "JN78"), 2),
    (("HB9",), ("JN47", "JN36", "JN46"), 3),
    (("SP5", "SP3", "SQ9"), ("KO02", "JO91", "KN09"), 3),
    (("OK1", "OK2"), ("JO70", "JN79", "JN89"), 2),
    (("OM3",), ("JN98", "KN08"), 1),
    (("LY2",), ("KO24",), 1),
    (("OH2", "OH6"), ("KP20", "KP11"), 1),
    (("SM5", "SM6"), ("JO99", "JO57"), 2),
    (("LA9",), ("JO59",), 1),
    (("OZ1",), ("JO55", "JO65"), 1),
    (("EI5",), ("IO63",), 1),
    (("GM4",), ("IO75", "IO85"), 1),
    (("GW4",), ("IO81",), 1),
    (("CT1", "CS7"), ("IM58", "IN51"), 2),
    (("9A2",), ("JN75", "JN85"), 1),
    (("S51",), ("JN76",), 1),
    (("HA5",), ("JN97",), 1),
    (("YO3",), ("KN34",), 1),
    (("SV1",), ("KM17",), 1),
    (("UR5",), ("KO50",), 1),
    (("LX1",), ("JN39",), 1),
    (("TK5",), ("JN41",), 1),
    (("EA8",), ("IL18",), 1),
    (("IT9",), ("JM77",), 1),
    (("K1", "W5", "N2", "K4"), ("FN42", "EM12", "FN31", "EM73", "DM04"), 3),
    (("VE3", "VE2"), ("FN03", "FN35"), 1),
    (("JA1", "JH1"), ("PM95",), 1),
    (("VK2",), ("QF56",), 1),
    (("PY2",), ("GG66",), 1),
    (("ZS6",), ("KG33",), 1),
    (("4X1",), ("KM72",), 1),
    (("FR4",), ("LG78",), 1),
)
_LETTERS = "ABCDEFGHIJKLMNOPRSTUVWXYZ"
_FIRST = ("Alain", "Anna", "Bernard", "Claire", "David", "Elena", "Frank", "Greta", "Hans",
          "Ines", "Jan", "Karin", "Luc", "Marta", "Nico", "Olga", "Paul", "Rita", "Sven", "Tomas")
_LAST = ("Martin", "Muller", "Smith", "Peeters", "Jansen", "Rossi", "Garcia", "Novak",
         "Berg", "Silva", "Dupont", "Keller", "Nowak", "Moreau", "Evans", "Lindqvist")

# Operating sessions: (operator, band, mode, start in hours from now, minutes, QSO/h)
_SESSIONS: tuple[tuple[str, str, str, float, int, int], ...] = (
    ("F4ABC", "40M", "SSB", -70.0, 90, 42),
    ("M0DEMO1", "20M", "CW", -66.5, 60, 30),
    ("TM0DEMO2", "20M", "FT8", -51.0, 120, 26),
    ("TM0ADM11", "15M", "SSB", -46.0, 60, 48),
    ("TM0SADM1", "2M", "FM", -44.2, 12, 30),
    ("F5XYZ", "40M", "CW", -28.0, 90, 32),
    ("M0DEMO1", "17M", "SSB", -26.0, 45, 36),
    ("TM0ADM11", "10M", "FT8", -22.5, 90, 30),
    ("F4ABC", "80M", "SSB", -20.0, 60, 40),
    ("TM0SADM1", "20M", "SSB", -4.0, 50, 55),
    ("TM0DEMO2", "40M", "SSB", -0.8, 47, 52),      # live right now
)
# Booked slots: (operator, band, mode, start in hours from now, minutes)
_PLANNED: tuple[tuple[str, str, str, float, int], ...] = (
    ("TM0DEMO2", "40M", "SSB", -1.0, 150),          # the live session's slot
    ("M0DEMO1", "40M", "CW", 2.0, 120),
    ("F4ABC", "80M", "SSB", 8.0, 90),
    ("TM0SADM1", "2M", "FM", 13.5, 15),
    ("F5XYZ", "15M", "FT8", 20.0, 120),
    ("TM0ADM11", "30M", "CW", 26.0, 90),
    ("M0DEMO1", "20M", "SSB", 44.0, 120),
)
_FREQ = {  # (band, mode family) → (low, high) MHz
    ("80M", "SSB"): (3.650, 3.780), ("40M", "SSB"): (7.080, 7.190), ("40M", "CW"): (7.005, 7.035),
    ("30M", "CW"): (10.105, 10.125), ("20M", "SSB"): (14.150, 14.300), ("20M", "CW"): (14.010, 14.060),
    ("20M", "FT8"): (14.074, 14.074), ("17M", "SSB"): (18.120, 18.160), ("15M", "SSB"): (21.200, 21.400),
    ("15M", "FT8"): (21.074, 21.074), ("10M", "FT8"): (28.074, 28.074), ("2M", "FM"): (145.850, 145.850),
}


def _pick_hunter(rng: random.Random) -> tuple[str, str]:
    prefixes, grids, _w = rng.choices(_HUNTERS, weights=[h[2] for h in _HUNTERS])[0]
    prefix = rng.choice(prefixes)
    if not prefix[-1].isdigit():
        prefix += str(rng.randint(1, 9))
    call = prefix + "".join(rng.choice(_LETTERS) for _i in range(rng.choice((2, 3, 3))))
    grid = rng.choice(grids) + rng.choice("ABCDEFGHIJKLMNOPQRSTUVWX") + rng.choice("ABCDEFGHIJKLMNOPQRSTUVWX")
    return call, grid.upper()


def _rst(mode: str, rng: random.Random) -> tuple[str, str]:
    if mode == "CW":
        return rng.choice(("599", "579", "589")), rng.choice(("599", "559", "579"))
    if mode in ("FT8", "FT4"):
        return f"{rng.randint(-20, 5):+d}", f"{rng.randint(-20, 5):+d}"
    return rng.choice(("59", "57", "58")), rng.choice(("59", "55", "57"))


def seed(now: datetime | None = None, rng: random.Random | None = None) -> dict[str, int]:
    """Fill the CURRENT activation paths with the demo data set.

    Only called on a scratch folder (``--seed``) or on isolated paths (tests).
    """
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    rng = rng or random.Random(int(now.timestamp()) // 3600)
    a = activation
    a.init_db()
    cs = a.callsign()
    today = now.date()
    a.update_station(
        cs, label=str(activation_config().get("label") or "Station de démonstration"),
        gridsquare=a.my_gridsquare() or "JN18DU",
        start_date=(today - timedelta(days=3)).isoformat(), end_date=(today + timedelta(days=4)).isoformat(),
        public=1, badge="DEMO", subtitle="", flags="fr",
    )

    # Settings: accounts mode (the demo accounts have their own password),
    # new accounts wait for an administrator, public pages complete.
    for key, value in (("per_operator_auth", True), ("operator_approval", True), ("show_contacts", True),
                       ("show_map_stats", True), ("auto_slots", True), ("slot_lock", True)):
        a.set_flag(key, value)
    a.set_certificate_options({"enabled": "1", "names": "1", "ranking": "1", "appendix": "1",
                               "border": "1", "emblem": "1", "emblem_text": "DEMO",
                               "ham_symbol": "1", "flag": "auto", "max_qso": "8"})

    # Accounts: the four demo ones, two plain club operators, one pending request.
    pw_hash = a.hash_password(password())
    created = int(now.timestamp())
    with a.conn() as c:
        for call, role in ACCOUNTS:
            c.execute(
                "INSERT INTO operators(callsign, name, active, password_hash, is_admin, is_superadmin, "
                "status, created_at) VALUES (?, '', 1, ?, ?, ?, 'active', ?) "
                "ON CONFLICT(callsign) DO UPDATE SET password_hash=excluded.password_hash, "
                "is_admin=excluded.is_admin, is_superadmin=excluded.is_superadmin, status='active', active=1",
                (call, pw_hash, int(role in ("admin", "superadmin")), int(role == "superadmin"), created),
            )
        for call in ("F4ABC", "F5XYZ"):
            c.execute("INSERT OR IGNORE INTO operators(callsign, name, active, created_at) VALUES (?, '', 1, ?)",
                      (call, created))
        c.execute(
            "INSERT OR IGNORE INTO operators(callsign, name, active, password_hash, status, created_at) "
            "VALUES ('F8NEW', '', 1, ?, 'pending', ?)", (a.hash_password(secrets.token_urlsafe(24)), created),
        )

    # Booked slots first: the log then attaches its sessions to them.
    quarter = now.replace(minute=now.minute // 15 * 15)
    for op, band, mode, start_h, minutes in _PLANNED:
        start = quarter + timedelta(hours=start_h)
        end = start + timedelta(minutes=minutes)
        a.add_slot(op, _iso(start), _iso(end), band, mode, "")

    # Hunters: a pool drawn once, so that some of them work the station on
    # several bands and modes (ranking, certificates).
    pool: dict[str, str] = {}
    while len(pool) < 170:
        call, grid = _pick_hunter(rng)
        pool.setdefault(call, grid)
    regulars = list(pool)[:25]
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for op, band, mode, start_h, minutes, rate in _SESSIONS:
        start = now + timedelta(hours=start_h)
        count = max(1, round(rate * minutes / 60))
        for i in range(count):
            t = start + timedelta(minutes=minutes * i / count + rng.random())
            if t > now:
                break
            call = rng.choice(regulars) if rng.random() < 0.3 else rng.choice(list(pool))
            if (call, band, mode) in seen:
                continue
            seen.add((call, band, mode))
            low, high = _FREQ[(band, mode)]
            rs, rr = _rst(mode, rng)
            grid = pool[call] if mode in ("FT8", "FT4") else (pool[call] if rng.random() < 0.6 else "")
            rows.append((
                cs, op, call, t.strftime("%Y%m%d"), t.strftime("%H%M"), band, mode,
                round(rng.uniform(low, high), 4), rs, rr,
                grid[:4] if mode in ("FT8", "FT4") else grid,
                "SO-50" if band == "2M" else "", "", created,
            ))
    with a.conn() as c:
        c.executemany(
            "INSERT INTO contacts(station, operator_call, call, qso_date, time_on, band, mode, freq_mhz, "
            "rst_sent, rst_rcvd, gridsquare, sat_name, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
        )
        # Callbook as QRZ would have filled it (the demo never queries QRZ).
        for call, grid in pool.items():
            if rng.random() < 0.7:
                _code, name = dxcc_flags.entity_for_call(call)
                c.execute(
                    "INSERT OR REPLACE INTO callbook(call, status, fname, name, grid, country, dxcc_name, "
                    "fetched_at, image, image_at) VALUES (?, 'ok', ?, ?, ?, ?, ?, ?, '', ?)",
                    (call, rng.choice(_FIRST), rng.choice(_LAST), grid, name, name, created, created),
                )
    a.reconcile_slots_from_log(force=True)
    return {"contacts": len(rows), "hunters": len({r[2] for r in rows})}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _seed_process(folder: str) -> None:
    """Entry point of the seed subprocess: every path goes to ``folder``."""
    out = Path(folder).resolve()
    if not out.is_dir() or out == activation.DB_PATH.parent.resolve():
        sys.exit("demo seed: scratch folder required")
    a = activation
    a.DB_PATH = out / "activation.sqlite"
    a.SETTINGS_FILE = out / "activation_settings.json"
    a.OP_PASSWORD_FILE = out / "activation_password"
    a.QRZ_ACCOUNT_FILE = out / "activation_qrz.json"
    a.BACKUP_DIR = out / "backups"
    a.maybe_backup = lambda: None
    a.backup_now = lambda: out / "none"
    counts = seed()
    print(json.dumps(counts))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--seed":
        _seed_process(sys.argv[2])
    else:
        sys.exit("usage: python -m app.demo --seed DIR")
