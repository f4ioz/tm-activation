"""Login log (admin and operators) for anti-bruteforce protection.

Reduced version of the original site's visit log: only login
attempts are kept (``var/auth_log.sqlite``, 90 days),
so that ``security.login_blocked()`` counts failures per IP and the
Settings page shows them. No visits are logged.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "var" / "auth_log.sqlite"
RETENTION_DAYS = 90

logger = logging.getLogger(__name__)

# Typical paths probed by bots looking for vulnerabilities (the app has none
# of these): security.py blocks the IP after a few such requests.
_SCAN_RE = re.compile(
    r"wp-|wordpress|xmlrpc|phpmyadmin|\.php|\.env|\.git|\.aws|/cgi-bin|/boaform|/hnap1|"
    r"/actuator|/vendor/|/owa/|/solr|/manager/html|/admin\.|/shell|/setup\.cgi|\.asp",
    re.I,
)


def client_ip(request: Any) -> str:
    """Visitor IP.

    Behind the reverse proxy, ProxyHeadersMiddleware (main.py) has already
    replaced the proxy's address with the visitor's — only if the proxy
    is listed in ``server.trusted_proxies``: an X-Forwarded-For sent
    directly by a client is ignored (no spoofing of LAN IPs).
    """
    return (request.client.host if request.client else "")[:64]


def get_override(ip: str) -> dict[str, Any] | None:
    """IPs labelled by the admin (never blocked): not handled here."""
    return None


_schema_ready: set[str] = set()


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    if str(DB_PATH) in _schema_ready:
        return
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ip TEXT,
                kind TEXT,                      -- admin / operator
                callsign TEXT DEFAULT '',
                success INTEGER,
                ua TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_auth_ts ON auth_events(ts);
            CREATE INDEX IF NOT EXISTS idx_auth_ip ON auth_events(ip, ts);
            """
        )
    _schema_ready.add(str(DB_PATH))


def record_auth(request: Any, kind: str, callsign: str, success: bool) -> None:
    """Login attempt (admin or operator): never blocking, never fatal."""
    try:
        init_db()
        now = int(time.time())
        with conn() as c:
            c.execute(
                "INSERT INTO auth_events(ts, ip, kind, callsign, success, ua) VALUES (?, ?, ?, ?, ?, ?)",
                (now, client_ip(request), kind, (callsign or "")[:16].upper(), 1 if success else 0,
                 request.headers.get("user-agent", "")[:300]),
            )
            c.execute("DELETE FROM auth_events WHERE ts < ?", (now - RETENTION_DAYS * 86400,))
    except Exception:  # noqa: BLE001
        logger.debug("journal des connexions : événement ignoré", exc_info=True)


def failed_logins(ip: str, since: int) -> int:
    """Failed logins (admin + operators) from an IP since ``since``."""
    init_db()
    with conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM auth_events WHERE ip = ? AND success = 0 AND ts >= ?",
                             (ip, since)).fetchone()[0])


def auth_counts(days: int) -> dict[str, int]:
    """Successful / failed logins over the last ``days`` days."""
    init_db()
    since = int(time.time()) - days * 86400
    with conn() as c:
        counts = {int(r[0]): int(r[1]) for r in c.execute(
            "SELECT success, COUNT(*) FROM auth_events WHERE ts >= ? GROUP BY success", (since,))}
    return {"ok": counts.get(1, 0), "failed": counts.get(0, 0)}


def auth_log(days: int = 7, limit: int = 200, failures_only: bool = False) -> list[dict[str, Any]]:
    """Login log, most recent first (monitoring page)."""
    init_db()
    since = int(time.time()) - days * 86400
    sql = "SELECT ts, ip, kind, callsign, success, ua FROM auth_events WHERE ts >= ?"
    if failures_only:
        sql += " AND success = 0"
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql, (since, int(limit)))]
    for r in rows:
        r["when"] = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%d/%m/%Y %H:%M")
        r["ua"] = (r["ua"] or "")[:60]
    return rows


def failed_by_ip(days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
    """IPs with the most failures (password attempts)."""
    init_db()
    since = int(time.time()) - days * 86400
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT ip, COUNT(*) AS n, MAX(ts) AS last FROM auth_events "
            "WHERE ts >= ? AND success = 0 GROUP BY ip ORDER BY n DESC, last DESC LIMIT ?",
            (since, int(limit)))]
    for r in rows:
        r["last_str"] = datetime.fromtimestamp(r["last"], timezone.utc).strftime("%d/%m/%Y %H:%M")
    return rows


def quick_summary() -> dict[str, Any]:
    """« Connexions » box in the Settings: last 7 days."""
    init_db()
    since = int(time.time()) - 7 * 86400
    with conn() as c:
        counts = {
            int(r[0]): int(r[1])
            for r in c.execute("SELECT success, COUNT(*) FROM auth_events WHERE ts >= ? GROUP BY success",
                               (since,))
        }
        recent = [dict(r) for r in c.execute(
            "SELECT ts, ip, kind, callsign FROM auth_events WHERE ts >= ? AND success = 0 "
            "ORDER BY ts DESC, id DESC LIMIT 10", (since,))]
    for r in recent:
        r["when"] = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%d/%m %H:%M")
        r["kind"] = {"admin": "admin", "operator": "opérateur"}.get(r["kind"], r["kind"])
    return {"logins_7d": counts.get(1, 0), "failed_logins_7d": counts.get(0, 0), "recent_failures": recent}
