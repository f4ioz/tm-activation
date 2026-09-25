"""Site hardening: headers, safe redirects, rate limits, automatic blocking
of scanners and password-guessing attacks.

Seen in the production logs (Sept. 2026): ~2,200 requests/day from scanners
(WordPress, .env, .git, .php), the FastAPI docs being fetched, and a
password-guessing attack on /login (123 failures on 3 September).

Blocking state is kept in memory (a single uvicorn worker): lost on restart,
which is fine for temporary bans. Login failures are read from the persistent
log (visits.auth_events).
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

SCAN_THRESHOLD = 5          # scan requests (wp-, .env, .php…) within SCAN_WINDOW → ban
SCAN_WINDOW = 600
BAN_SECONDS = 3600
LOGIN_MAX_FAILURES = 5      # login failures (admin + operators) per IP within LOGIN_WINDOW
LOGIN_WINDOW = 900
FAILED_LOGIN_DELAY = 1.0    # seconds to wait after a failure (slows down guessing)
QRZ_PUBLIC_LIMIT = 30       # public /api/qrz/lookup: 30 lookups / 10 min / IP
QRZ_PUBLIC_WINDOW = 600

SECURITY_HEADERS = {
    # Geolocation (/grid) and microphone (/sstv) restricted to the site itself.
    "Permissions-Policy": "geolocation=(self), microphone=(self), camera=(), payment=(), usb=()",
    # Minimal policy, compatible with the site's inline and CDN scripts.
    "Content-Security-Policy": "object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'",
}
_NO_STORE_PREFIXES = ("/admin", "/activation", "/login")  # private pages: never cached

_lock = threading.Lock()
_scan_hits: dict[str, deque[float]] = {}
_bans: dict[str, dict[str, Any]] = {}
_rate: dict[str, deque[float]] = {}


def reset() -> None:
    """Forget bans and counters (tests)."""
    with _lock:
        _scan_hits.clear()
        _bans.clear()
        _rate.clear()


# Local networks, never banned. Explicit list: Python's ``ip.is_private`` also
# includes the documentation ranges (203.0.113.0/24…), which must be treated as
# public IPs.
_LAN_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "::1/128", "fe80::/10", "fc00::/7",
)]


def _public_ip(ip: str) -> bool:
    """Valid IP outside the local network (the LAN and invalid IPs are never banned)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not any(addr.version == net.version and addr in net for net in _LAN_NETS)


def safe_next(value: str | None, default: str = "/", prefix: str = "/") -> str:
    """Internal redirect target only.

    Rejects "//host", "/\\host" and control characters (browsers strip tabs
    and newlines: "/\\t/host" becomes "//host"), and anything that does not
    start with ``prefix``.
    """
    v = value or ""
    if (not v.startswith(prefix) or v.startswith("//") or "\\" in v
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in v)):
        return default
    return v


def is_https(request: Any) -> bool:
    """Request received over HTTPS (nginx → X-Forwarded-Proto): "Secure" cookies."""
    return request.url.scheme == "https"


def rate_limited(key: str, limit: int, window: float) -> bool:
    """True if ``key`` has already made ``limit`` requests within the window."""
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
    """Too many recent login failures from this IP (password guessing)?"""
    if not _public_ip(ip):
        return False
    return visits.failed_logins(ip, int(time.time() - LOGIN_WINDOW)) >= LOGIN_MAX_FAILURES


def _prune(now: float) -> None:
    for store in (_scan_hits, _rate):
        if len(store) > 5000:
            for k in [k for k, q in store.items() if not q or q[-1] < now - max(SCAN_WINDOW, QRZ_PUBLIC_WINDOW)]:
                del store[k]


def check_request(request: Any) -> Response | None:
    """Scanner blocking: 403 if the IP is banned, otherwise None.

    A public IP that requests ``SCAN_THRESHOLD`` exploit paths within
    ``SCAN_WINDOW`` seconds is banned from the whole site for ``BAN_SECONDS``.
    Never banned: the LAN, the logged-in admin, IPs labelled by the admin.
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
        # /static/ excluded: DXCC flags are served from /static/vendor/flags/
        # ("/vendor/" is a scan pattern → visitors of /tm25test would get banned)
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
    """Currently banned IPs (admin page)."""
    now = time.time()
    with _lock:
        rows = [{"ip": ip, **b, "remaining": int(b["until"] - now)}
                for ip, b in _bans.items() if b["until"] > now]
    return sorted(rows, key=lambda b: -b["since"])
