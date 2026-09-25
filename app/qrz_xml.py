"""QRZ XML API client (paid subscribers).

The XML API exposes an authentication endpoint that returns a `Session.Key`
valid for ~24 h, which is then used on lookup requests. The client renews
the token automatically when it expires (or on a 401/Invalid session error).

Configuration (config.yml):
    qrz:
      username: F1ABC
      password: ...

No cookie to handle by hand.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from app.config import qrz_config

logger = logging.getLogger(__name__)

XML_BASE = "https://xmldata.qrz.com/xml/current/"
USER_AGENT = "tm-activation/1.0"
NS = "{http://xmldata.qrz.com}"
SESSION_TTL = 23 * 3600          # renewed proactively before 24 h
REQ_TIMEOUT = 10.0
DEFAULT_LOOKUP_TTL = 6 * 3600    # per-callsign lookup cache (positive + negative)


@dataclass
class XmlLookup:
    call: str
    name: str = ""
    qth: str = ""
    country: str = ""
    grid: str = ""
    lat: float | None = None
    lon: float | None = None
    image: str = ""
    profile_url: str = ""
    fname: str = ""
    lname: str = ""
    dxcc: str = ""       # DXCC entity number
    land: str = ""       # DXCC entity name (≠ country = postal country)
    cqzone: str = ""


@dataclass
class _CacheEntry:
    fetched_at: float
    result: XmlLookup | None  # None = negative (callsign not found)


def _strip_ns(tag: str) -> str:
    return tag[len(NS):] if tag.startswith(NS) else tag


def _xml_to_dict(elem: ET.Element) -> dict[str, str]:
    return {_strip_ns(child.tag): (child.text or "").strip() for child in elem}


class QrzXmlClient:
    """Thread-safe client with lazy login + automatic renewal."""

    def __init__(self, username: str, password: str,
                 agent: str = USER_AGENT,
                 lookup_ttl_seconds: float = DEFAULT_LOOKUP_TTL):
        self.username = username
        self.password = password
        self.agent = agent
        self._key: str | None = None
        self._key_expires: float = 0.0
        self.sub_exp: str = ""       # XML subscription end date reported by QRZ at login
        self._lock = threading.Lock()
        self._cache: dict[str, _CacheEntry] = {}
        self._lookup_ttl = lookup_ttl_seconds

    # ── session ────────────────────────────────────────────────────────

    def _login(self, client: httpx.Client) -> str:
        params = {
            "username": self.username,
            "password": self.password,
            "agent": self.agent,
        }
        r = client.get(XML_BASE, params=params)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        sess = root.find(f"{NS}Session")
        if sess is None:
            raise RuntimeError("QRZ XML: <Session> manquante")
        d = _xml_to_dict(sess)
        if "Error" in d and d["Error"]:
            raise RuntimeError(f"QRZ XML login: {d['Error']}")
        key = d.get("Key", "")
        if not key:
            raise RuntimeError("QRZ XML: pas de Key dans la réponse de login")
        self._key = key
        self._key_expires = time.time() + SESSION_TTL
        sub_exp = d.get("SubExp", "")
        self.sub_exp = sub_exp
        logger.info("QRZ XML login OK (sub expires %s)", sub_exp or "?")
        return key

    def check_login(self) -> tuple[str, str]:
        """Log in right away, to validate an entered account.

        ``("ok", subscription end)``; ``("refused", QRZ message)`` if the
        account is refused; ``("error", reason)`` if QRZ is unreachable.
        """
        with self._lock, httpx.Client(timeout=REQ_TIMEOUT) as client:
            self._key = None
            try:
                self._login(client)
            except RuntimeError as exc:
                return "refused", str(exc).removeprefix("QRZ XML login: ")
            except (httpx.HTTPError, ET.ParseError) as exc:
                return "error", str(exc)
        return "ok", self.sub_exp

    def _ensure_key(self, client: httpx.Client) -> str:
        if self._key and time.time() < self._key_expires:
            return self._key
        return self._login(client)

    # ── public API ─────────────────────────────────────────────────────

    def lookup(self, callsign: str) -> XmlLookup | None:
        return self.lookup_with_status(callsign)[0]

    def lookup_with_status(self, callsign: str) -> tuple[XmlLookup | None, str]:
        """Like lookup() but tells "not found" apart from an error.

        Status ``ok`` / ``notfound`` / ``error``: a background task must not
        record a network failure as an unknown callsign.
        """
        cs = callsign.strip().upper()
        if not cs:
            return None, "notfound"
        with self._lock:
            entry = self._cache.get(cs)
            if entry is not None and (time.time() - entry.fetched_at) < self._lookup_ttl:
                return entry.result, ("ok" if entry.result else "notfound")
            with httpx.Client(timeout=REQ_TIMEOUT) as client:
                try:
                    key = self._ensure_key(client)
                    params = {"s": key, "callsign": cs}
                    r = client.get(XML_BASE, params=params)
                    r.raise_for_status()
                    root = ET.fromstring(r.text)

                    # Session error? Retry after a forced login
                    sess = root.find(f"{NS}Session")
                    if sess is not None:
                        d = _xml_to_dict(sess)
                        err = d.get("Error", "").lower()
                        if "invalid session" in err or "session timeout" in err:
                            logger.info("QRZ XML session expirée, re-login")
                            self._key = None
                            key = self._login(client)
                            r = client.get(XML_BASE,
                                           params={"s": key, "callsign": cs})
                            r.raise_for_status()
                            root = ET.fromstring(r.text)

                    cs_node = root.find(f"{NS}Callsign")
                    if cs_node is None:
                        err = ""
                        sess = root.find(f"{NS}Session")
                        if sess is not None:
                            err = _xml_to_dict(sess).get("Error", "")
                            if err:
                                logger.info("QRZ XML lookup %s: %s", cs, err)
                        if err and "not found" not in err.lower():
                            # Account / quota error, not an unknown callsign
                            return None, "error"
                        # Negative cache: don't hit QRZ again for TTL
                        self._cache[cs] = _CacheEntry(time.time(), None)
                        return None, "notfound"

                    d = _xml_to_dict(cs_node)
                    lat = lon = None
                    try:
                        lat = float(d["lat"]) if d.get("lat") else None
                        lon = float(d["lon"]) if d.get("lon") else None
                    except ValueError:
                        pass
                    fname = d.get("fname", "")
                    name = d.get("name", "")
                    full_name = (fname + " " + name).strip()
                    qth_bits = [d.get(k, "") for k in ("addr1", "addr2", "state")]
                    qth = ", ".join(b for b in qth_bits if b)
                    result = XmlLookup(
                        call=d.get("call", cs).upper(),
                        name=full_name,
                        qth=qth,
                        country=d.get("country", ""),
                        grid=d.get("grid", "").upper(),
                        lat=lat, lon=lon,
                        image=d.get("image", ""),
                        profile_url=f"https://www.qrz.com/db/{cs}",
                        fname=fname,
                        lname=name,
                        dxcc=d.get("dxcc", ""),
                        land=d.get("land", ""),
                        cqzone=d.get("cqzone", ""),
                    )
                    self._cache[cs] = _CacheEntry(time.time(), result)
                    return result, "ok"
                except (httpx.HTTPError, ET.ParseError, RuntimeError) as exc:
                    logger.warning("QRZ XML lookup %s failed: %s", cs, exc)
                    # No negative cache on network error: retry next time
                    return None, "error"

    def clear_cache(self) -> None:
        """Clear the lookup cache (useful for debugging or after a quota change)."""
        with self._lock:
            self._cache.clear()


_shared: QrzXmlClient | None = None
_shared_lock = threading.Lock()


def get_shared_client() -> QrzXmlClient | None:
    """Single XML client for the whole site (one QRZ session, one cache).

    None if ``qrz.username`` / ``qrz.password`` are not configured.
    """
    global _shared
    cfg = qrz_config()
    user = cfg.get("username") or cfg.get("xml_username")
    pwd = cfg.get("password") or cfg.get("xml_password")
    if not user or not pwd:
        return None
    with _shared_lock:
        if _shared is None:
            ttl = float(cfg.get("ttl_seconds", DEFAULT_LOOKUP_TTL))
            _shared = QrzXmlClient(user, pwd, lookup_ttl_seconds=ttl)
        return _shared
