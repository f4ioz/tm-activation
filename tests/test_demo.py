"""Demo mode (install.sh --demo): data set, banner, restrictions, reset."""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import activation, demo, i18n, security
from app.templating import templates
from app.main import app

PW = demo.DEFAULT_PASSWORD
PUBLIC_PEER = ("203.0.113.9", 50000)


def _captcha() -> dict[str, str]:
    ts, good = str(int(time.time()) - 5), 7
    return {"captcha_token": f"{ts}.{activation._captcha_sig(ts, good)}", "captcha": str(good)}


def _login(client: TestClient, call: str, password: str = PW):
    return client.post("/activation/login", data={"callsign": call, "password": password, **_captcha()})


@pytest.fixture
def demo_on(monkeypatch, tmp_path):
    """Demo enabled on isolated paths; the data-layer wrappers are undone after."""
    saved = dict(vars(activation))
    monkeypatch.setattr(demo, "demo_config", lambda: {"enabled": True, "reset_hours": 24})
    monkeypatch.setattr(demo, "MARKER_FILE", tmp_path / "DEMO_INSTANCE")
    monkeypatch.setattr(demo, "STATE_FILE", tmp_path / "demo_state.json")
    monkeypatch.setattr(demo, "WORK_DIR", tmp_path / "demo-seed")
    monkeypatch.setattr(demo, "_installed", False)
    monkeypatch.setattr(demo, "_names_installed", False)
    monkeypatch.setitem(templates.env.globals, "club", {"name": demo.DEMO_CLUB, "callsign": "", "city": "",
                                                        "website": ""})
    monkeypatch.setattr(activation, "LOGO_DIR", tmp_path / "branding")
    monkeypatch.setattr(activation, "QRZ_ACCOUNT_FILE", tmp_path / "activation_qrz.json")
    demo.seed()
    demo.install_limits()
    demo.install_translated_names()
    yield
    for name, value in saved.items():
        if getattr(activation, name, None) is not value:
            setattr(activation, name, value)


def _client() -> TestClient:
    return TestClient(app, follow_redirects=False, client=PUBLIC_PEER)


# ── Data set ───────────────────────────────────────────────────────────────


def test_off_by_default() -> None:
    assert demo.info() is None
    assert demo.reset_allowed() is False
    assert demo.reset_now() is False                     # never without the demo config
    r = TestClient(app).get("/activations")
    assert "demo-banner" not in r.text


def test_seed_is_representative(demo_on) -> None:
    stats = activation.stats()
    assert stats["total"] > 250
    entities = activation.worked_entities()
    assert len(entities) >= 20                           # Europe + some DX
    ranking = activation.hunters_ranking(5)
    assert ranking and ranking[0]["qsos"] >= 2           # regulars on several bands
    assert activation.live_slots(), "a session must be live right now"
    assert activation.future_slots()
    roles = {o["callsign"]: o for o in activation.list_operators(active_only=False)}
    assert all(call in roles for call in demo.PROTECTED)
    assert roles["TM0SADM1"]["is_superadmin"] and roles["TM0ADM11"]["is_admin"]
    assert not roles["M0DEMO1"]["is_admin"] and roles["F8NEW"]["status"] == "pending"
    for call in demo.PROTECTED:
        assert activation.operator_login(call, PW) == "ok"
    assert activation.per_operator_auth() and activation.is_public()


def test_banner_and_countdown(demo_on) -> None:
    assert 'id="demo-count"' not in _client().get("/activation/login").text   # no reset → no countdown
    demo.MARKER_FILE.write_text("demo", encoding="utf-8")
    demo._write_state(time.time() - 3600)
    client = _client()
    page = client.get("/activation/login")
    assert "demo-banner" in page.text and "TM0SADM1" in page.text and PW in page.text
    assert 'id="demo-count"' in page.text and "<details open>" in page.text
    public = client.get(f"/{activation.current_station()['slug']}")
    assert "demo-banner" in public.text
    assert public.headers["x-robots-tag"] == "noindex, nofollow"
    info = demo.info()
    assert 22 * 3600 < info["remaining"] <= 23 * 3600


def test_roles_work_with_demo_accounts(demo_on) -> None:
    op = _client()
    assert _login(op, "M0DEMO1").status_code == 303
    assert op.get("/activation/log").status_code == 200
    assert op.get("/activation/settings").status_code == 303        # not a superadmin
    sa = _client()
    assert _login(sa, "TM0SADM1").status_code == 303
    assert sa.get("/activation/settings").status_code == 200


# ── Restrictions ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path, data", [
    ("/activation/settings/password", {"new_password": "x", "confirm": "x"}),
    ("/activation/settings/auth", {}),
    ("/activation/settings/qrz", {"username": "F1ABC", "password": "secret"}),
    ("/activation/settings/backup", {}),
    ("/activation/settings/logo/delete", {}),
    ("/activation/settings/operators/TM0DEMO2", {"action": "disable"}),
    ("/activation/settings/operators/m0demo1", {"action": "password", "password": "Hack-123!"}),
])
def test_superadmin_cannot_break_the_demo(demo_on, path, data) -> None:
    sa = _client()
    _login(sa, "TM0SADM1")
    r = sa.post(path, data=data)
    assert r.status_code == 403
    assert activation.per_operator_auth()
    assert activation.operator_login("TM0DEMO2", PW) == "ok"
    assert activation.operator_login("M0DEMO1", PW) == "ok"


def test_backup_download_refused(demo_on) -> None:
    sa = _client()
    _login(sa, "TM0SADM1")
    assert sa.get("/activation/settings/backup.sqlite").status_code == 403


def test_protected_accounts_at_the_data_layer(demo_on) -> None:
    for fn in (activation.set_operator_active, activation.set_operator_admin, activation.set_operator_superadmin):
        with pytest.raises(ValueError):
            fn("TM0ADM11", False)
    with pytest.raises(ValueError):
        activation.set_operator_password_for("TM0SADM1", "Other-123!")
    with pytest.raises(ValueError):
        activation.clear_operator_password("M0DEMO1")
    with pytest.raises(ValueError):
        activation.set_qrz_account("F1ABC", "secret")
    assert activation.qrz_client() is None
    # A visitor's own account stays manageable by the demo admins.
    activation.approve_operator("F8NEW")
    activation.set_operator_admin("F8NEW", True)
    assert activation.operator_is_admin("F8NEW")


def test_certificate_qr_code_cannot_be_changed(demo_on) -> None:
    before = activation.get_certificate_options()["qr_url"]
    activation.set_certificate_options({"enabled": "1", "qr_url": "https://phishing.example/x"})
    assert activation.get_certificate_options()["qr_url"] == before


def test_body_size_and_rate_limits(demo_on) -> None:
    op = _client()
    _login(op, "M0DEMO1")
    big = op.post("/activation/slots", data={"note": "x" * (demo.MAX_BODY + 10)})
    assert big.status_code == 413
    security.reset()
    codes = [op.post("/activation/whoami", data={"operator": "M0DEMO1"}).status_code
             for _ in range(demo.POSTS_PER_MINUTE + 1)]
    assert codes[-1] == 429 and codes[0] == 303


def test_caps(demo_on, monkeypatch) -> None:
    monkeypatch.setattr(demo, "MAX_CONTACTS", 10)
    monkeypatch.setattr(demo, "MAX_SLOTS", 1)
    monkeypatch.setattr(demo, "MAX_OPERATORS", 1)
    op = _client()
    _login(op, "TM0ADM11")
    r = op.post("/activation/contacts", data={"call": "DL1ABC", "band": "20M", "mode": "CW"},
                headers={"HX-Request": "true"})
    assert "act-alert-err" in r.text
    with pytest.raises(ValueError):
        activation.add_slot("M0DEMO1", "2030-01-01T10:00", "2030-01-01T11:00", "20M", "CW")
    assert activation.operator_login("F9NEWBIE", "Newbie-123!") == "disabled"
    assert activation.operator_login("M0DEMO1", PW) == "ok"     # existing accounts still log in


# ── Reset ──────────────────────────────────────────────────────────────────


def test_reset_needs_the_installation_marker(demo_on) -> None:
    before = activation.stats()["total"]
    assert demo.reset_now() is False            # config says demo, but no var/DEMO_INSTANCE
    assert activation.stats()["total"] == before


def test_reset_rebuilds_the_data(demo_on) -> None:
    demo.MARKER_FILE.write_text("demo", encoding="utf-8")
    activation.add_contact(call="ZZ9ZZZ", band="20M", mode="CW", operator_call="TM0ADM11")
    activation.set_flag("show_contacts", False)
    with activation.conn() as c:
        c.execute("DELETE FROM operators WHERE callsign='TM0SADM1'")
    assert demo.reset_now() is True
    assert not activation.contacts_for_call("ZZ9ZZZ")["total"]
    assert activation.show_contacts()
    assert activation.operator_login("TM0SADM1", PW) == "ok"
    assert demo.last_reset() > time.time() - 60
    assert not demo.WORK_DIR.exists()
    # The live database was overwritten in place (no file swap under the server).
    with sqlite3.connect(activation.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] > 250


# ── Names in the visitor's language ────────────────────────────────────────


def _raw_label() -> str:
    with sqlite3.connect(activation.DB_PATH) as c:
        return c.execute("SELECT label FROM stations WHERE callsign=?", (activation.callsign(),)).fetchone()[0]


def test_station_label_follows_the_language(demo_on) -> None:
    i18n.use("en")
    try:
        assert activation.current_station()["label"] == "Demo station"
        assert activation.label() == "Demo station"
        assert activation.list_stations()[0]["label"] == "Demo station"
    finally:
        i18n.use("fr")
    assert activation.current_station()["label"] == demo.DEMO_LABEL
    assert _raw_label() == demo.DEMO_LABEL               # the database keeps the key


def test_pages_show_translated_names(demo_on) -> None:
    slug = activation.current_station()["slug"]
    en = TestClient(app).get(f"/{slug}", headers={"Accept-Language": "en"}).text
    assert "Demo station" in en and "Demo radio club" in en and demo.DEMO_LABEL not in en
    fr = TestClient(app).get(f"/{slug}", headers={"Accept-Language": "fr"}).text
    assert demo.DEMO_LABEL in fr and demo.DEMO_CLUB in fr


def test_saving_in_english_keeps_the_french_key(demo_on) -> None:
    cs = activation.callsign()
    i18n.use("en")
    try:
        activation.update_station(cs, label="Demo station", public=1)
        assert _raw_label() == demo.DEMO_LABEL
        activation.update_station(cs, public=1)            # label not given: unchanged
        assert _raw_label() == demo.DEMO_LABEL
        activation.update_station(cs, label="Field Day 2026", public=1)
        assert activation.current_station()["label"] == "Field Day 2026"   # a visitor's own label
    finally:
        i18n.use("fr")
    assert _raw_label() == "Field Day 2026"
