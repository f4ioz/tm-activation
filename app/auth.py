"""Authentification administrateur.

Pas de comptes : un seul mot de passe admin défini dans config.yml
(``auth.password``). À la connexion, on pose un cookie HMAC signé avec un
secret persistant (var/auth_secret). L'admin accède aux Réglages de l'espace
activation (indicatifs, mot de passe opérateurs, points, sauvegardes).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

from fastapi import Request

from app.config import load_config

ROOT = Path(__file__).resolve().parent.parent
SECRET_FILE = ROOT / "var" / "auth_secret"

COOKIE_NAME = "tm_admin"
TOKEN_TTL = 12 * 3600  # 12 h


def _secret() -> bytes:
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SECRET_FILE.is_file():
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_bytes()


def auth_password() -> str:
    return (load_config().get("auth", {}) or {}).get("password", "") or ""


def get_secret() -> bytes:
    """Secret HMAC persistant (partagé pour signer les cookies de session)."""
    return _secret()


def make_token(now: int | None = None) -> str:
    ts = int(time.time()) if now is None else int(now)
    body = str(ts).encode()
    sig = hmac.new(_secret(), body, hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    ts_str, sig = token.split(".", 1)
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > TOKEN_TTL:
        return False
    expected = hmac.new(_secret(), ts_str.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def is_private(request: Request) -> bool:
    """Administrateur connecté pour la requête courante (cookie valide)."""
    if not auth_password():
        return False
    return verify_token(request.cookies.get(COOKIE_NAME))


def auth_enabled() -> bool:
    """Mot de passe admin configuré → la connexion admin est possible."""
    return bool(auth_password())
