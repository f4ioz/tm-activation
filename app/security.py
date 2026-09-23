"""Durcissement du site : en-têtes, redirections sûres, limites de débit,
blocage automatique des scanners et des attaques sur les mots de passe.

Constats dans les logs de prod (sept. 2026) : ~2 200 requêtes/jour de
scanners (WordPress, .env, .git, .php), la doc FastAPI récupérée, et une
attaque par essais de mots de passe sur /login (123 échecs le 3 septembre).

État de blocage en mémoire (un seul worker uvicorn) : perdu au redémarrage,
ce qui convient à des blocages temporaires. Les échecs de connexion sont
lus dans le journal persistant (visits.auth_events).
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque
from typing import Any

from fastapi.responses import PlainTextResponse, Response

from app import auth as _auth
from app import visits

SCAN_THRESHOLD = 5          # requêtes de scan (wp-, .env, .php…) en SCAN_WINDOW → blocage
SCAN_WINDOW = 600
BAN_SECONDS = 3600
LOGIN_MAX_FAILURES = 5      # échecs de connexion (admin + opérateurs) par IP en LOGIN_WINDOW
LOGIN_WINDOW = 900
FAILED_LOGIN_DELAY = 1.0    # secondes d'attente après un échec (ralentit les essais)
QRZ_PUBLIC_LIMIT = 30       # /api/qrz/lookup pour le public : 30 recherches / 10 min / IP
QRZ_PUBLIC_WINDOW = 600

SECURITY_HEADERS = {
    # Géolocalisation (/grid) et micro (/sstv) réservés au site lui-même.
    "Permissions-Policy": "geolocation=(self), microphone=(self), camera=(), payment=(), usb=()",
    # Politique minimale compatible avec les scripts inline et CDN du site.
    "Content-Security-Policy": "object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'",
}
_NO_STORE_PREFIXES = ("/admin", "/activation", "/login")  # pages privées : jamais en cache

_lock = threading.Lock()
_scan_hits: dict[str, deque[float]] = {}
_bans: dict[str, dict[str, Any]] = {}
_rate: dict[str, deque[float]] = {}


def reset() -> None:
    """Oublie blocages et compteurs (tests)."""
    with _lock:
        _scan_hits.clear()
        _bans.clear()
        _rate.clear()


# Réseaux locaux jamais bloqués. Liste explicite : ``ip.is_private`` de Python
# inclut aussi les plages de documentation (203.0.113.0/24…), à traiter comme
# des IP publiques.
_LAN_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "::1/128", "fe80::/10", "fc00::/7",
)]


def _public_ip(ip: str) -> bool:
    """IP valide hors réseau local (le LAN et les IP invalides ne sont jamais bloqués)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not any(addr.version == net.version and addr in net for net in _LAN_NETS)


def safe_next(value: str | None, default: str = "/", prefix: str = "/") -> str:
    """Cible de redirection interne uniquement.

    Refuse « //hôte », « /\\hôte » et les caractères de contrôle (les
    navigateurs suppriment tabulations et retours ligne : « /\\t/hôte »
    devient « //hôte »), et tout ce qui ne commence pas par ``prefix``.
    """
    v = value or ""
    if (not v.startswith(prefix) or v.startswith("//") or "\\" in v
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in v)):
        return default
    return v


def is_https(request: Any) -> bool:
    """Requête arrivée en HTTPS (nginx → X-Forwarded-Proto) : cookies « Secure »."""
    return request.url.scheme == "https"


def rate_limited(key: str, limit: int, window: float) -> bool:
    """True si ``key`` a déjà fait ``limit`` requêtes dans la fenêtre."""
    now = time.time()
    with _lock:
        q = _rate.setdefault(key, deque())
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


def login_blocked(ip: str) -> bool:
    """Trop d'échecs de connexion récents depuis cette IP (essais de mots de passe) ?"""
    if not _public_ip(ip):
        return False
    return visits.failed_logins(ip, int(time.time() - LOGIN_WINDOW)) >= LOGIN_MAX_FAILURES


def _prune(now: float) -> None:
    for store in (_scan_hits, _rate):
        if len(store) > 5000:
            for k in [k for k, q in store.items() if not q or q[-1] < now - max(SCAN_WINDOW, QRZ_PUBLIC_WINDOW)]:
                del store[k]


def check_request(request: Any) -> Response | None:
    """Blocage des scanners : 403 si l'IP est bloquée, sinon None.

    Une IP publique qui demande ``SCAN_THRESHOLD`` chemins de failles en
    ``SCAN_WINDOW`` secondes est bloquée ``BAN_SECONDS`` sur tout le site.
    Jamais bloqués : le LAN, l'admin connecté, les IP étiquetées par l'admin.
    """
    ip = visits.client_ip(request)
    if not _public_ip(ip) or _auth.is_private(request):
        return None
    now = time.time()
    path = request.url.path
    with _lock:
        ban = _bans.get(ip)
        if ban and ban["until"] > now:
            ban["blocked"] += 1
            return PlainTextResponse("Accès temporairement bloqué.", status_code=403)
        if ban:
            del _bans[ip]
        # /static/ exclu : les drapeaux DXCC sont servis depuis /static/vendor/flags/
        # (« /vendor/ » est un motif de scan → bannissement des visiteurs de /tm25test)
        if path.startswith("/static/") or not visits._SCAN_RE.search(path):
            return None
        q = _scan_hits.setdefault(ip, deque())
        q.append(now)
        while q and q[0] < now - SCAN_WINDOW:
            q.popleft()
        hits = len(q)
        _prune(now)
    if hits >= SCAN_THRESHOLD and not visits.get_override(ip):
        with _lock:
            _bans[ip] = {"until": now + BAN_SECONDS, "since": now, "blocked": 0,
                         "reason": f"{hits} requêtes de scan en {SCAN_WINDOW // 60} min", "sample": path}
            _scan_hits.pop(ip, None)
    return None


def add_headers(request: Any, response: Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith(_NO_STORE_PREFIXES):
        response.headers["Cache-Control"] = "no-store"


def banned_ips() -> list[dict[str, Any]]:
    """IP actuellement bloquées (page admin)."""
    now = time.time()
    with _lock:
        rows = [{"ip": ip, **b, "remaining": int(b["until"] - now)}
                for ip, b in _bans.items() if b["until"] > now]
    return sorted(rows, key=lambda b: -b["since"])
