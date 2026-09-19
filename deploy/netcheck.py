"""Contrôles réseau d'install.sh (bibliothèque standard uniquement).

    python3 netcheck.py local            IP locale, carte réseau, adresse MAC
    python3 netcheck.py public           adresse IPv4 publique de la connexion
    python3 netcheck.py dns DOMAINE      enregistrements A et AAAA (DNS publics)
    python3 netcheck.py cert DOMAINE [HÔTE]   certificat HTTPS (défaut : cette machine)

Sortie : lignes CLE=valeur, lues par install.sh sans être exécutées.
"""

from __future__ import annotations

import ipaddress
import json
import random
import socket
import ssl
import struct
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

PUBLIC_IP_URLS = ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://v4.ident.me")
# Résolveurs publics interrogés directement : pas le cache de la box, qui garde
# plusieurs minutes un nom « introuvable » juste avant sa création.
DNS_SERVERS = ("1.1.1.1", "8.8.8.8")
CGNAT = ipaddress.ip_network("100.64.0.0/10")
TYPE_A, TYPE_AAAA = 1, 28


def local_info() -> dict[str, str]:
    """Adresse locale utilisée pour sortir sur Internet, sa carte réseau et sa MAC."""
    ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # aucun paquet envoyé : choix de l'interface de sortie
            ip = s.getsockname()[0]
    except OSError:
        pass
    iface = mac = ""
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=5).stdout
        for link in json.loads(out or "[]"):
            if any(a.get("local") == ip for a in link.get("addr_info", [])):
                iface = link.get("ifname", "")
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if iface:
        try:
            with open(f"/sys/class/net/{iface}/address", encoding="ascii") as f:
                mac = f.read().strip()
        except OSError:
            pass
    return {"LOCAL_IP": ip, "IFACE": iface, "MAC": mac}


def classify(ip: str) -> str:
    """« public », « cgnat » (100.64.0.0/10, partagée par l'opérateur), « private » ou ""."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    if addr in CGNAT:
        return "cgnat"
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return "private"
    return "public"


def public_ip(urls: tuple[str, ...] = PUBLIC_IP_URLS, timeout: float = 5.0) -> str:
    """Adresse IPv4 publique vue d'Internet ("" si aucun service ne répond)."""
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tm-activation-install"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                addr = ipaddress.ip_address(r.read(64).decode("ascii", "replace").strip())
        except (OSError, ValueError):
            continue
        if addr.version == 4:
            return str(addr)
    return ""


# ── Requête DNS minimale (UDP) ─────────────────────────────────────────────


def _qname(domain: str) -> bytes:
    labels = domain.strip().rstrip(".").encode("idna").split(b".")
    return b"".join(bytes([len(label)]) + label for label in labels) + b"\0"


def _skip_name(buf: bytes, pos: int) -> int:
    while True:
        n = buf[pos]
        if n == 0:
            return pos + 1
        if n & 0xC0 == 0xC0:  # pointeur de compression
            return pos + 2
        pos += 1 + n


def parse_response(buf: bytes, qtype: int) -> list[str]:
    """Adresses du type demandé dans la section réponse (chaînes CNAME comprises)."""
    qdcount, ancount = struct.unpack(">HH", buf[4:8])
    pos = 12
    for _ in range(qdcount):
        pos = _skip_name(buf, pos) + 4
    found = set()
    for _ in range(ancount):
        pos = _skip_name(buf, pos)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", buf[pos:pos + 10])
        pos += 10
        rdata = buf[pos:pos + rdlen]
        pos += rdlen
        if rtype == qtype == TYPE_A and rdlen == 4:
            found.add(str(ipaddress.IPv4Address(rdata)))
        elif rtype == qtype == TYPE_AAAA and rdlen == 16:
            found.add(str(ipaddress.IPv6Address(rdata)))
    return sorted(found)


def query(domain: str, qtype: int, server: str, timeout: float = 3.0) -> list[str]:
    qid = random.randrange(1 << 16)
    msg = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + _qname(domain) + struct.pack(">HH", qtype, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(msg, (server, 53))
        buf = s.recv(4096)
    if len(buf) < 12 or buf[:2] != msg[:2]:
        raise OSError("réponse DNS inattendue")
    rcode = buf[3] & 0x0F
    if rcode == 3:  # NXDOMAIN : le nom n'existe pas
        return []
    if rcode != 0:
        raise OSError(f"erreur DNS {rcode}")
    return parse_response(buf, qtype)


def _system_lookup(domain: str, family: int) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(domain, None, family, socket.SOCK_STREAM)})
    except (OSError, UnicodeError):
        return []


def dns(domain: str) -> dict[str, str]:
    for server in DNS_SERVERS:
        try:
            return {
                "DNS_A": ",".join(query(domain, TYPE_A, server)),
                "DNS_AAAA": ",".join(query(domain, TYPE_AAAA, server)),
                "DNS_VIA": server,
            }
        except (OSError, IndexError, struct.error, UnicodeError):
            continue
    return {  # DNS publics injoignables (réseau filtré) : résolveur du système
        "DNS_A": ",".join(_system_lookup(domain, socket.AF_INET)),
        "DNS_AAAA": ",".join(_system_lookup(domain, socket.AF_INET6)),
        "DNS_VIA": "système",
    }


def cert(domain: str, host: str = "127.0.0.1", port: int = 443, timeout: float = 5.0) -> dict[str, str]:
    """Certificat HTTPS que nginx sert pour ``domain`` sur cette machine."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                info = tls.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        return {"CERT": "invalid", "CERT_DAYS": "", "CERT_ERROR": exc.verify_message or "certificat refusé"}
    except OSError:
        return {"CERT": "none", "CERT_DAYS": "", "CERT_ERROR": "pas de HTTPS sur cette machine"}
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), timezone.utc)
    return {"CERT": "ok", "CERT_DAYS": str((expires - datetime.now(timezone.utc)).days), "CERT_ERROR": ""}


def _clean(value: object) -> str:
    return "".join(ch for ch in str(value) if ch.isprintable()).strip()


def main(argv: list[str]) -> int:
    cmd, args = (argv[1] if len(argv) > 1 else ""), argv[2:]
    if cmd == "local" and not args:
        out = local_info()
    elif cmd == "public" and not args:
        ip = public_ip()
        out = {"PUBLIC_IP": ip, "PUBLIC_KIND": classify(ip)}
    elif cmd == "dns" and len(args) == 1:
        out = dns(args[0])
    elif cmd == "cert" and 1 <= len(args) <= 2:  # cert DOMAINE [HÔTE] (tunnel : l'hôte est le domaine)
        out = cert(*args)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for key, value in out.items():
        print(f"{key}={_clean(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
