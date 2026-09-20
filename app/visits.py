"""Journal des connexions (admin et opérateurs) pour la protection anti-bruteforce.

Version réduite du journal des visites du site d'origine : seules les
tentatives de connexion sont conservées (``var/auth_log.sqlite``, 90 jours),
pour que ``security.login_blocked()`` compte les échecs par IP et que les
Réglages les affichent. Aucune visite n'est journalisée.
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

# Chemins typiques des robots qui cherchent une faille (l'application n'a rien
# de tout ça) : security.py bloque l'IP après quelques requêtes de ce genre.
_SCAN_RE = re.compile(
    r"wp-|wordpress|xmlrpc|phpmyadmin|\.php|\.env|\.git|\.aws|/cgi-bin|/boaform|/hnap1|"
    r"/actuator|/vendor/|/owa/|/solr|/manager/html|/admin\.|/shell|/setup\.cgi|\.asp",
    re.I,
)


def client_ip(request: Any) -> str:
    """IP du visiteur.

    Derrière le reverse proxy, ProxyHeadersMiddleware (main.py) a déjà
    remplacé l'adresse du proxy par celle du visiteur — uniquement si le proxy
    figure dans ``server.trusted_proxies`` : un X-Forwarded-For envoyé
    directement par un client est ignoré (pas d'usurpation d'IP du LAN).
    """
    return (request.client.host if request.client else "")[:64]


def get_override(ip: str) -> dict[str, Any] | None:
    """IP étiquetées par l'admin (jamais bloquées) : non gérées ici."""
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
    """Tentative de connexion (admin ou opérateur) : jamais bloquant, jamais fatal."""
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
    """Échecs de connexion (admin + opérateurs) d'une IP depuis ``since``."""
    init_db()
    with conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM auth_events WHERE ip = ? AND success = 0 AND ts >= ?",
                             (ip, since)).fetchone()[0])


def auth_counts(days: int) -> dict[str, int]:
    """Connexions réussies / ratées sur les ``days`` derniers jours."""
    init_db()
    since = int(time.time()) - days * 86400
    with conn() as c:
        counts = {int(r[0]): int(r[1]) for r in c.execute(
            "SELECT success, COUNT(*) FROM auth_events WHERE ts >= ? GROUP BY success", (since,))}
    return {"ok": counts.get(1, 0), "failed": counts.get(0, 0)}


def auth_log(days: int = 7, limit: int = 200, failures_only: bool = False) -> list[dict[str, Any]]:
    """Journal des connexions, le plus récent d'abord (page de surveillance)."""
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
    """IP qui ont le plus échoué (essais de mots de passe)."""
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
    """Encart « Connexions » des Réglages : 7 derniers jours."""
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
