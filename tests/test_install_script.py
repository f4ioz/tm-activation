"""Guided installation (install.sh): functions tested by sourcing the script.

The parts that need root (packages, systemd, nginx, certbot) do not run
here; we check the questions, the router guide and the diagnostics, with
simulated answers and fake network checks.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Fake network checks (replace deploy/netcheck.py).
STUB = """
netcheck() {
  case "$1" in
    local) NC_LOCAL_IP=192.168.1.42 NC_IFACE=eth0 NC_MAC=d8:3a:dd:12:34:56 ;;
    public) NC_PUBLIC_IP=82.64.10.20 NC_PUBLIC_KIND=public ;;
    dns) NC_DNS_A="${FAKE_A-82.64.10.20}" NC_DNS_AAAA="${FAKE_AAAA-}" ;;
  esac
}
"""


def run(script: str, stdin: str = "", env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source ./install.sh\n{script}"],
        cwd=ROOT, input=stdin, capture_output=True, text=True, timeout=30,
        env={**os.environ, **(env or {})},
    )


def fake_cloudflared(tmp_path: Path, uuid: str = "8f1c2d3e-4b5a-6c7d-8e9f-0a1b2c3d4e5f") -> dict[str, str]:
    """Fake cloudflared + working directories, to exercise setup_tunnel."""
    bin_dir, login, conf = tmp_path / "bin", tmp_path / "login", tmp_path / "etc"
    bin_dir.mkdir()
    login.mkdir()
    (bin_dir / "cloudflared").write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$CALLS"\n'
        'case "$*" in\n'
        f'  *"tunnel list"*) echo \'[{{"id":"{uuid}","name":"tm-tm-example-org"}}]\' ;;\n'
        "esac\nexit 0\n"
    )
    (bin_dir / "cloudflared").chmod(0o755)
    (login / "cert.pem").write_text("faux certificat d'origine")
    (login / f"{uuid}.json").write_text("{}")
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(tmp_path / "calls"),
            "CF_CONF_DIR": str(conf), "CF_LOGIN_DIR": str(login)}


def test_no_apostrophe_inside_parameter_expansion() -> None:
    """« "${1:-c'est}" »: bash takes the apostrophe for a quote and the whole
    script becomes unparseable (syntax error far away from it)."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert not re.search(r"\$\{[^}]*'[^}]*\}", text)
    assert subprocess.run(["bash", "-n", "install.sh"], cwd=ROOT).returncode == 0


def test_sourcing_does_nothing() -> None:
    r = run('echo "sourcé $VERSION"')
    version = (ROOT / "VERSION").read_text().strip()
    assert r.returncode == 0 and r.stdout.strip() == f"sourcé {version}"


def test_help_lists_guided_options() -> None:
    r = subprocess.run(["bash", "install.sh", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and "--check" in r.stdout and "--box" in r.stdout


def test_ask_mode_internet_with_freebox() -> None:
    r = run('ask_mode; echo "R=$MODE|$DOMAIN|$BOX|$EMAIL"', stdin="2\nTM.Example.org\n1\nvous@exemple.fr\n")
    assert r.returncode == 0
    assert "R=internet|tm.example.org|freebox|vous@exemple.fr" in r.stdout


def test_ask_mode_rejects_bad_domain_and_email() -> None:
    r = run('ask_mode; echo "R=$MODE|$DOMAIN|$BOX|$EMAIL"',
            stdin="2\nmauvais domaine\ntm.example.org\n\npas-un-email\n\n")
    assert "Nom de domaine invalide" in r.stdout and "E-mail invalide" in r.stdout
    assert "R=internet|tm.example.org|freebox|" in r.stdout


def test_collect_config_questions() -> None:
    answers = "\n".join([
        "tm50abc", "50 ans du club", "jn18fs",                      # station
        "Radio-club de Test", "f6zzz", "Testville", "",             # club
        "", "f1aaa, f4bbb",                                         # address, operators
        "secret", "autre", "secret", "secret",                      # admin: wrong, then right
        "",                                                          # operators: later
        "",                                                          # no QRZ
    ]) + "\n"
    r = run('MODE=internet DOMAIN=tm.example.org INTERACTIVE=yes PORT=8000 PORT_SUFFIX=:8000\n'
            'collect_config\n'
            'echo "R=$CALLSIGN|$GRID|$CLUB_CALLSIGN|$BASE_URL|$OPERATORS|$ADMIN_PASSWORD|$OPERATOR_PASSWORD"',
            stdin=answers)
    assert r.returncode == 0, r.stderr
    assert "Les deux saisies diffèrent" in r.stdout
    assert "R=TM50ABC|JN18FS|F6ZZZ|https://tm.example.org|f1aaa, f4bbb|secret|" in r.stdout


def test_recap_can_abort_without_changes() -> None:
    r = run("MODE=internet DOMAIN=tm.example.org BOX=freebox FIRST_CONFIG=1 CALLSIGN=TM50ABC\n"
            "DIR=/opt/tm-activation SERVICE=tm-activation\nrecap\necho SUITE", stdin="n\n")
    assert r.returncode == 0
    assert "Freebox" in r.stdout and "TM50ABC" in r.stdout and "Rien n'a été modifié" in r.stdout
    assert "SUITE" not in r.stdout


def test_freebox_guide_all_good() -> None:
    r = run(STUB + "BOX=freebox DOMAIN=tm.example.org\nnetwork_guide\necho SHARED=$SHARED_IP",
            stdin="\n\n\n\n")
    out = r.stdout
    assert r.returncode == 0, r.stderr
    assert "Baux statiques" in out and "d8:3a:dd:12:34:56" in out and "192.168.1.42" in out
    assert "full-stack" in out
    assert "tm.example.org." in out and "82.64.10.20" in out
    assert "DNS correct" in out
    assert "Gestion des ports" in out and "443" in out
    assert "SHARED=0" in out


def test_freebox_guide_stops_on_shared_ipv4() -> None:
    r = run(STUB + "BOX=freebox DOMAIN=tm.example.org\nnetwork_guide\necho SHARED=$SHARED_IP",
            stdin="\nn\n")
    assert "SHARED=1" in r.stdout and "Gestion des ports" not in r.stdout


def test_guide_dns_retry_then_continue() -> None:
    r = run(STUB + "FAKE_A= FAKE_AAAA=2a01:e0a::1 BOX=freebox DOMAIN=tm.example.org\nnetwork_guide",
            stdin="\n\n\n1\n\n2\n\n")
    out = r.stdout
    assert r.returncode == 0, r.stderr
    assert out.count("n'existe pas encore") == 2
    assert "AAAA (IPv6) présent" in out
    assert "Gestion des ports" in out


@pytest.mark.parametrize("certbot_out, hint", [
    ("  Detail: 82.64.10.20: Fetching http://tm.example.org/.well-known/acme-challenge/x: "
     "Timeout during connect (likely firewall problem)", "Port 80 injoignable"),
    ("  Detail: DNS problem: NXDOMAIN looking up A for tm.example.org", "pas (encore) dans le DNS"),
    ("  Detail: 2a01:e0a:1:2::3: Fetching http://tm.example.org/: Timeout during connect", "AAAA"),
    ("Error: too many failed authorizations recently", "attendez une heure"),
    ("  Detail: 82.64.10.20: Connection refused", "Port 80 fermé"),
])
def test_certbot_failures_are_explained(certbot_out: str, hint: str) -> None:
    r = run(f"BOX=freebox DOMAIN=tm.example.org\nexplain_certbot {certbot_out!r}")
    assert r.returncode == 0 and hint in r.stdout


def test_box_instructions_differ() -> None:
    r = run(STUB + "netcheck local\nfor BOX in livebox sfr bbox autre; do help_ports; done")
    assert "NAT/PAT" in r.stdout and "Réseau v4" in r.stdout and r.stdout.count("192.168.1.42") >= 4


@pytest.mark.parametrize("args", ["--tunnel --domain tm.example.org", "--domain tm.example.org --tunnel"])
def test_tunnel_flag_wins_over_domain(args: str) -> None:
    r = run(f'parse_args {args}; echo "R=$CLI_MODE|$CLI_DOMAIN"')
    assert "R=tunnel|tm.example.org" in r.stdout


def test_ask_mode_tunnel_asks_only_the_domain() -> None:
    r = run('ask_mode; echo "R=$MODE|$DOMAIN|$BOX|$EMAIL"', stdin="3\nTM.Example.org\n")
    assert "R=tunnel|tm.example.org||" in r.stdout


def test_tunnel_name_defaults_to_domain() -> None:
    r = run('DOMAIN=tm.example.org; tunnel_name; TUNNEL_NAME=perso; tunnel_name')
    assert r.stdout.split() == ["tm-tm-example-org", "perso"]


@pytest.mark.parametrize("name, ok", [("tm-club", True), ("nom invalide", False), ("tm;rm", False)])
def test_tunnel_name_is_validated(name: str, ok: bool) -> None:
    r = run("MODE=tunnel DOMAIN=tm.example.org USE_SYSTEMD=yes PORT=8000 SERVICE=tm-activation\n"
            f'SVC_USER=tmact TUNNEL_NAME={name!r}\nvalidate && echo VALIDE')
    assert ("VALIDE" in r.stdout) is ok
    if not ok:
        assert "nom de tunnel invalide" in r.stderr


def test_tunnel_guide_stops_without_cloudflare_domain() -> None:
    r = run("MODE=tunnel DOMAIN=tm.example.org\ntunnel_guide\necho READY=$TUNNEL_READY", stdin="n\n")
    assert "dash.cloudflare.com" in r.stdout and "READY=0" in r.stdout


def test_setup_tunnel_writes_config_and_route(tmp_path: Path) -> None:
    env = fake_cloudflared(tmp_path)
    r = run('systemctl() { echo "systemctl $*" >> "$CALLS"; }\n'
            "MODE=tunnel DOMAIN=tm.example.org PORT=8000 TUNNEL_READY=1\nsetup_tunnel",
            env=env)
    assert r.returncode == 0, r.stderr
    config = (tmp_path / "etc" / "config.yml").read_text()
    assert "tunnel: 8f1c2d3e-4b5a-6c7d-8e9f-0a1b2c3d4e5f" in config
    assert "hostname: tm.example.org" in config
    assert "service: http://127.0.0.1:8000" in config
    assert "service: http_status:404" in config
    calls = (tmp_path / "calls").read_text()
    assert "ingress validate" in calls                                  # config validated
    assert "route dns tm-tm-example-org tm.example.org" in calls        # name routed to the tunnel
    assert "systemctl restart cloudflared" in calls
    assert (tmp_path / "etc" / "8f1c2d3e-4b5a-6c7d-8e9f-0a1b2c3d4e5f.json").exists()


def test_setup_tunnel_skipped_when_domain_not_on_cloudflare(tmp_path: Path) -> None:
    env = fake_cloudflared(tmp_path)
    r = run("MODE=tunnel DOMAIN=tm.example.org PORT=8000 TUNNEL_READY=0\nsetup_tunnel", env=env)
    assert r.returncode == 0 and not (tmp_path / "etc").exists()


def test_unknown_box_is_refused() -> None:
    r = subprocess.run(["bash", "install.sh", "--box", "minitel", "--check"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 1 and "box inconnue" in r.stderr
