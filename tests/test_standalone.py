"""Tests specific to the standalone app: root, admin, proxy, club branding."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import activation, auth as auth_mod, config, security, templating
from app.main import app
from app.routers import activation as activation_router

PUBLIC_PEER = ("203.0.113.5", 50000)   # public IP (documentation range)
PROXY_PEER = ("127.0.0.1", 50000)      # local nginx (trusted proxy by default)


@pytest.fixture
def admin_pw(monkeypatch):
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "adminpw")


def _admin_client() -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(auth_mod.COOKIE_NAME, auth_mod.make_token())
    return client


def _fail(client: TestClient, n: int, **kw) -> list[int]:
    return [client.post("/login", data={"password": "faux"}, **kw).status_code for _ in range(n)]


def test_home_redirects_to_list_then_to_public_page() -> None:
    client = TestClient(app, follow_redirects=False)
    r = client.get("/")
    assert r.status_code == 302 and r.headers["location"] == "/activations"
    st = activation.update_station(activation.callsign(), public=True)
    assert client.get("/").headers["location"] == f"/{st['slug']}"
    assert client.get(f"/{st['slug']}").status_code == 200


def test_robots_and_healthz() -> None:
    client = TestClient(app)
    assert "Disallow: /activation/" in client.get("/robots.txt").text
    h = client.get("/healthz").json()
    assert h["status"] == "ok" and re.match(r"^\d+\.\d+\.\d+$|^dev$", h["version"])
    assert h["station"] == activation.callsign()
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_admin_login_flow(admin_pw) -> None:
    client = TestClient(app, follow_redirects=False)
    r = client.get("/activation/settings")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/login").status_code == 200
    assert client.post("/login", data={"password": "faux"}).status_code == 401
    ok = client.post("/login", data={"password": "adminpw", "next": "/activation/settings"})
    assert ok.status_code == 303 and ok.headers["location"] == "/activation/settings"
    page = client.get("/activation/settings")
    assert page.status_code == 200
    assert "Connexions" in page.text and "1 connexion(s) ratée(s)" in page.text
    assert "Fréquentation" not in page.text
    client.post("/logout")
    assert client.get("/activation/settings").status_code == 303


def test_login_redirect_stays_internal(admin_pw) -> None:
    client = TestClient(app, follow_redirects=False)
    r = client.post("/login", data={"password": "adminpw", "next": "//evil.example/"})
    assert r.headers["location"] == "/activation/settings"


def test_admin_without_password_configured() -> None:
    client = TestClient(app, follow_redirects=False)
    r = client.post("/login", data={"password": "n'importe"})
    assert r.status_code == 401 and "auth.password" in r.text


def test_bruteforce_blocked_for_public_ip(admin_pw) -> None:
    client = TestClient(app, follow_redirects=False, client=PUBLIC_PEER)
    assert _fail(client, 5) == [401] * 5
    # Blocked, even with the right password.
    assert client.post("/login", data={"password": "adminpw"}).status_code == 429


def test_spoofed_forwarded_for_is_ignored(admin_pw) -> None:
    """A direct client cannot pretend to be on the LAN (never blocked)."""
    client = TestClient(app, follow_redirects=False, client=PUBLIC_PEER)
    spoof = {"X-Forwarded-For": "192.168.1.10"}
    assert _fail(client, 5, headers=spoof) == [401] * 5
    assert client.post("/login", data={"password": "adminpw"}, headers=spoof).status_code == 429


def test_trusted_proxy_forwards_real_ip_and_https(admin_pw) -> None:
    client = TestClient(app, follow_redirects=False, client=PROXY_PEER)
    bad = {"X-Forwarded-For": "203.0.113.77", "X-Forwarded-Proto": "https"}
    assert _fail(client, 5, headers=bad) == [401] * 5
    assert client.post("/login", data={"password": "adminpw"}, headers=bad).status_code == 429
    # Another visitor behind the same proxy is not penalized; Secure cookie over https.
    other = {"X-Forwarded-For": "198.51.100.8", "X-Forwarded-Proto": "https"}
    ok = client.post("/login", data={"password": "adminpw"}, headers=other)
    assert ok.status_code == 303 and "secure" in ok.headers["set-cookie"].lower()


def test_local_web_assets_never_trigger_scan_ban() -> None:
    """htmx, Leaflet, fonts and flags are served from /static/vendor/:
    "/vendor/" is a scan pattern, a visitor must not get banned."""
    client = TestClient(app, follow_redirects=False, client=PUBLIC_PEER)
    for name in ("htmx/a.js", "leaflet/b.js", "leaflet/c.css", "fonts/d.woff2", "flags/fr.png", "flags/de.png"):
        client.get(f"/static/vendor/{name}")
    assert client.get("/activations").status_code == 200
    assert not security.banned_ips()


def test_lan_is_never_blocked(admin_pw) -> None:
    client = TestClient(app, follow_redirects=False, client=("192.168.1.20", 50000))
    assert _fail(client, 7) == [401] * 7


def test_club_branding(monkeypatch) -> None:
    club = {"callsign": "F1ZZZ", "name": "Radio-club de Test", "city": "Testville",
            "website": "https://club.example"}
    monkeypatch.setitem(templating.templates.env.globals, "club", club)
    monkeypatch.setattr(activation_router, "club_config", lambda: club)
    st = activation.update_station(activation.callsign(), public=True)
    client = TestClient(app)
    lst = client.get("/activations").text
    assert "Radio-club de Test" in lst and "Testville" in lst and "F1ZZZ" in lst
    assert 'href="https://club.example"' in client.get(f"/{st['slug']}").text
    assert "Espace opérateurs F1ZZZ" in client.get("/activation/login").text


def test_no_branding_of_the_origin_site(admin_pw) -> None:
    st = activation.update_station(activation.callsign(), public=True)
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F1AAA")
    anon = TestClient(app)
    admin = _admin_client()
    pages = [anon.get(p) for p in ("/activations", f"/{st['slug']}", "/activation/login", "/login")]
    pages += [admin.get(p) for p in ("/activation", "/activation/planning", "/activation/log",
                                     "/activation/adif", "/activation/settings",
                                     f"/activation/stations/{st['slug']}/edit")]
    for r in pages:
        assert r.status_code == 200, r.url
        assert not re.search(r"(?i)f4ioz|/admin/visites", r.text), r.url


def test_web_assets_are_served_locally(admin_pw) -> None:
    """Local network without Internet: htmx, Leaflet and fonts served by the app."""
    st = activation.update_station(activation.callsign(), public=True)
    anon = TestClient(app)
    pub = anon.get(f"/{st['slug']}").text
    log = _admin_client().get("/activation/log").text
    for text in (pub, log):
        assert not re.search(r"unpkg\.com|fonts\.(googleapis|gstatic)\.com", text)
        assert "/static/vendor/fonts/fonts.css" in text
    assert "/static/vendor/leaflet/leaflet.js" in pub and "/static/vendor/leaflet/leaflet.css" in pub
    # Date picker: also served locally (planning).
    planning = _admin_client().get("/activation/planning").text
    for asset in ("/static/vendor/flatpickr/flatpickr.min.js", "/static/vendor/flatpickr/flatpickr.min.css",
                  "/static/vendor/flatpickr/fr.js", "/static/js/activation-datetime.js"):
        assert asset in planning, asset
        assert TestClient(app).get(asset.split("?")[0]).status_code == 200, asset
    assert "/static/vendor/htmx/htmx.min.js" in log
    for path in ("/static/vendor/htmx/htmx.min.js", "/static/vendor/leaflet/leaflet.js",
                 "/static/vendor/leaflet/leaflet.css", "/static/vendor/leaflet/images/layers.png"):
        assert anon.get(path).status_code == 200, path
    fonts = re.findall(r"url\(([\w.-]+\.woff2)\)", anon.get("/static/vendor/fonts/fonts.css").text)
    assert len(set(fonts)) >= 3
    assert all(anon.get(f"/static/vendor/fonts/{f}").status_code == 200 for f in set(fonts))


def test_adif_export_program_id() -> None:
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F1AAA")
    assert "<PROGRAMID:13>tm-activation" in activation.to_adif()


def test_config_path_from_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / "club.yml"
    path.write_text("club:\n  name: Club Env\n", encoding="utf-8")
    monkeypatch.setenv("TM_CONFIG", str(path))
    config.load_config.cache_clear()
    try:
        assert config.club_config() == {"name": "Club Env"}
        monkeypatch.setenv("TM_CONFIG", str(tmp_path / "absent.yml"))
        config.load_config.cache_clear()
        with pytest.raises(FileNotFoundError):
            config.load_config()
    finally:
        monkeypatch.delenv("TM_CONFIG")
        config.load_config.cache_clear()


def test_admin_pages_in_english(admin_pw) -> None:
    """Package-specific pages (admin login, login log) in English."""
    en = {"Accept-Language": "en-US,en;q=0.9"}
    anon = TestClient(app, follow_redirects=False, headers=en)
    login = anon.get("/login").text
    assert '<html lang="en">' in login and "Administrator login" in login and "Operator?" in login
    assert "Wrong password" in anon.post("/login", data={"password": "faux"}).text
    admin = _admin_client()
    admin.headers.update(en)
    page = admin.get("/activation/settings").text
    assert "Logins" in page and "1 failed login(s)" in page and "Log out (admin)" in page
    assert "Connexions" not in page and "connexion(s)" not in page


def test_surveillance_page(admin_pw) -> None:
    """Monitoring page: logins, blocked bots, admin only."""
    anon = TestClient(app, follow_redirects=False)
    r = anon.get("/admin/surveillance")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    # A few failed attempts + one successful login from a public IP.
    public = TestClient(app, follow_redirects=False, client=PUBLIC_PEER)
    _fail(public, 2)
    public.post("/login", data={"password": "adminpw"})
    page = _admin_client().get("/admin/surveillance")
    assert page.status_code == 200
    for expected in ("Surveillance", "Journal des connexions", "Robots bloqués",
                     PUBLIC_PEER[0], "échec", "réussie"):
        assert expected in page.text, expected
    # Filters: period and failures only.
    assert _admin_client().get("/admin/surveillance?days=30&failures=1").status_code == 200
    assert "Aucune IP bloquée" in page.text                      # nothing blocked yet
    # A scanner gets blocked: it shows up on the page.
    scanner = TestClient(app, follow_redirects=False, client=("203.0.113.9", 4000))
    for path in ("/wp-admin/setup-config.php", "/.env", "/vendor/phpunit/phpunit.php",
                 "/wp-login.php", "/.git/config", "/phpinfo.php"):
        scanner.get(path)
    page = _admin_client().get("/admin/surveillance").text
    assert "203.0.113.9" in page and "Aucune IP bloquée" not in page
