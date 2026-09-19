"""Contrôles réseau de l'installeur (deploy/netcheck.py), sans accès réseau."""

from __future__ import annotations

import importlib.util
import ipaddress
import socket
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("netcheck", ROOT / "deploy" / "netcheck.py")
netcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(netcheck)


def _response(qtype: int, answers: list[tuple[int, bytes]], rcode: int = 0) -> bytes:
    question = netcheck._qname("tm.example.org") + struct.pack(">HH", qtype, 1)
    header = b"\x12\x34" + struct.pack(">BBHHHH", 0x81, 0x80 | rcode, 1, len(answers), 0, 0)
    body = b"".join(b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 300, len(rdata)) + rdata
                    for rtype, rdata in answers)
    return header + question + body


class FakeUdp:
    """Socket UDP simulée : ``reply(message envoyé)`` donne la réponse."""

    def __init__(self, reply):
        self.reply = reply
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, _t):
        pass

    def sendto(self, msg, _addr):
        self.sent = msg

    def recv(self, _n):
        return self.reply(self.sent)


def test_qname_encoding() -> None:
    assert netcheck._qname("tm.mon-club.fr.") == b"\x02tm\x08mon-club\x02fr\x00"


def test_parse_a_through_cname_chain() -> None:
    buf = _response(1, [(5, netcheck._qname("monclub-tm.duckdns.org")), (1, bytes([82, 64, 10, 20]))])
    assert netcheck.parse_response(buf, 1) == ["82.64.10.20"]


def test_parse_aaaa_only_when_asked() -> None:
    buf = _response(28, [(28, ipaddress.IPv6Address("2a01:e0a::1").packed)])
    assert netcheck.parse_response(buf, 28) == ["2a01:e0a::1"]
    assert netcheck.parse_response(buf, 1) == []


def test_query_nxdomain_is_empty(monkeypatch) -> None:
    def reply(sent: bytes) -> bytes:  # même identifiant, NXDOMAIN, question recopiée
        return sent[:2] + bytes([0x81, 0x83]) + sent[4:6] + b"\0" * 6 + sent[12:]
    monkeypatch.setattr(netcheck.socket, "socket", lambda *a, **k: FakeUdp(reply))
    assert netcheck.query("absent.example.org", 1, "1.1.1.1") == []


def test_dns_answer_with_wrong_id_falls_back_to_system(monkeypatch) -> None:
    monkeypatch.setattr(netcheck.socket, "socket", lambda *a, **k: FakeUdp(lambda sent: b"\0\0" + sent[2:]))

    def fake_getaddrinfo(host, port, family, *rest):
        if family == socket.AF_INET:
            return [(family, socket.SOCK_STREAM, 6, "", ("82.64.10.20", 0))]
        raise socket.gaierror("pas d'AAAA")
    monkeypatch.setattr(netcheck.socket, "getaddrinfo", fake_getaddrinfo)
    assert netcheck.dns("tm.example.org") == {"DNS_A": "82.64.10.20", "DNS_AAAA": "", "DNS_VIA": "système"}


def test_dns_via_public_resolver(monkeypatch) -> None:
    def reply(sent: bytes) -> bytes:
        qtype = struct.unpack(">H", sent[-4:-2])[0]
        answers = [(1, bytes([82, 64, 10, 20]))] if qtype == 1 else []
        return sent[:2] + _response(qtype, answers)[2:]
    monkeypatch.setattr(netcheck.socket, "socket", lambda *a, **k: FakeUdp(reply))
    assert netcheck.dns("tm.example.org") == {"DNS_A": "82.64.10.20", "DNS_AAAA": "", "DNS_VIA": "1.1.1.1"}


def test_classify() -> None:
    assert netcheck.classify("100.64.12.1") == "cgnat"
    assert netcheck.classify("192.168.1.42") == "private"
    assert netcheck.classify("82.64.10.20") == "public"
    assert netcheck.classify("") == ""


def test_public_ip_ignores_ipv6_answers(monkeypatch) -> None:
    answers = iter([b"2a01:e0a::1\n", b"82.64.10.20\n"])

    class Resp:
        def __init__(self):
            self.body = next(answers)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _n):
            return self.body

    monkeypatch.setattr(netcheck.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert netcheck.public_ip(("https://a", "https://b")) == "82.64.10.20"


def test_cert_without_https(monkeypatch) -> None:
    def refuse(*_a, **_k):
        raise ConnectionRefusedError
    monkeypatch.setattr(netcheck.socket, "create_connection", refuse)
    assert netcheck.cert("tm.example.org")["CERT"] == "none"


def test_main_cert_accepts_a_remote_host(monkeypatch, capsys) -> None:
    monkeypatch.setattr(netcheck, "cert", lambda domain, host="127.0.0.1": {"CERT": f"{domain}@{host}"})
    assert netcheck.main(["netcheck", "cert", "tm.example.org", "tm.example.org"]) == 0
    assert "CERT=tm.example.org@tm.example.org" in capsys.readouterr().out


def test_main_prints_clean_key_values(monkeypatch, capsys) -> None:
    monkeypatch.setattr(netcheck, "dns", lambda d: {"DNS_A": "1.2.3.4", "DNS_AAAA": "x\ny"})
    assert netcheck.main(["netcheck", "dns", "tm.example.org"]) == 0
    assert capsys.readouterr().out.splitlines() == ["DNS_A=1.2.3.4", "DNS_AAAA=xy"]
    assert netcheck.main(["netcheck", "inconnu"]) == 2
