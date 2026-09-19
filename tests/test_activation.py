"""Tests du module d'activation d'indicatif temporaire (TM25TEST)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import activation
from app import auth as auth_mod
from app.main import app
from app import qrz_xml
from app.qrz_xml import QrzXmlClient, XmlLookup
from app.wavelog_client import parse_adif


_REAL_QRZ_CLIENT = activation.qrz_client   # avant sa neutralisation par _isolate


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Base SQLite temporaire + config d'activation déterministe."""
    monkeypatch.setattr(activation, "DB_PATH", tmp_path / "activation.sqlite")
    monkeypatch.setattr(activation, "OP_PASSWORD_FILE", tmp_path / "activation_password")
    monkeypatch.setattr(activation, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(activation, "SETTINGS_FILE", tmp_path / "activation_settings.json")
    monkeypatch.setattr(activation, "_last_backup_ts", 0.0)
    monkeypatch.setattr(
        activation,
        "activation_config",
        lambda: {
            "callsign": "TM25TEST",
            "label": "Test",
            "my_gridsquare": "JN18",
            "badge": "50 ANS",
            "flags": "fr,be",
            "operators": ["F4IOZ"],
        },
    )
    monkeypatch.setattr(activation, "qrz_client", lambda: None)  # jamais de vrai QRZ en test
    monkeypatch.setattr(activation, "QRZ_ACCOUNT_FILE", tmp_path / "activation_qrz.json")
    monkeypatch.setattr(activation, "_qrz_own", None)
    monkeypatch.setattr(activation, "IMPORT_TMP_DIR", tmp_path / "import")
    activation.init_db()
    yield


def _public(grid: str = "") -> None:
    """Met en ligne la page publique de l'indicatif en cours (+ locator station)."""
    activation.update_station("TM25TEST", public=True, **({"gridsquare": grid} if grid else {}))


# ── Couche données ─────────────────────────────────────────────────────────


def test_seed_and_add_operator() -> None:
    calls = {o["callsign"] for o in activation.list_operators()}
    assert "F4IOZ" in calls  # seed depuis la config
    activation.add_operator("f6abc", "Radioclub")
    assert "F6ABC" in {o["callsign"] for o in activation.list_operators()}


def test_active_operators_excludes_dormant_roster() -> None:
    activation.add_operator("F9ZZZ")  # au roster mais sans activité
    activation.add_slot("F5RRO", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F1OGV")
    active = set(activation.active_operators())
    assert active == {"F5RRO", "F1OGV"}
    assert "F4IOZ" not in active   # seedé au roster mais aucun créneau/QSO
    assert "F9ZZZ" not in active


def test_add_operator_rejects_bad_callsign() -> None:
    with pytest.raises(ValueError):
        activation.add_operator("!!bad!!")


def test_slot_conflict_detection() -> None:
    activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    # Chevauchement même bande → conflit
    assert activation.slot_conflicts("2026-09-07T11:00", "2026-09-07T13:00", "20M")
    # Autre bande → pas de conflit
    assert not activation.slot_conflicts("2026-09-07T11:00", "2026-09-07T13:00", "40M")
    # Adjacent (fin = début) → pas de conflit
    assert not activation.slot_conflicts("2026-09-07T12:00", "2026-09-07T13:00", "20M")


def test_add_slot_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        activation.add_slot("F4IOZ", "2026-09-07T12:00", "2026-09-07T10:00", "20M", "SSB")


def test_add_contact_and_dupe() -> None:
    activation.add_contact(
        call="dl1abc", band="20M", mode="SSB", operator_call="F4IOZ",
        qso_date="20260907", time_on="1015",
    )
    assert activation.stats()["total"] == 1
    assert activation.is_dupe("DL1ABC", "20M", "SSB")
    assert not activation.is_dupe("DL1ABC", "40M", "SSB")


def test_add_contact_rejects_bad_grid() -> None:
    with pytest.raises(ValueError):
        activation.add_contact(
            call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ",
            gridsquare="ZZ99",
        )


def test_paris_to_utc_conversion() -> None:
    # Été : Europe/Paris = UTC+2 → 12:00 local = 10:00 UTC
    assert activation.paris_local_to_utc_iso("2026-07-01T12:00") == "2026-07-01T10:00"
    assert activation.paris_local_to_utc_iso("garbage") is None


def test_display_modes_local_vs_utc() -> None:
    # Été : Paris = UTC+2. Un créneau stocké 10:00 UTC…
    assert activation.disp("2026-07-01T10:00", "utc").strftime("%H:%M") == "10:00"
    assert activation.disp("2026-07-01T10:00", "local").strftime("%H:%M") == "12:00"
    # Saisie interprétée dans le mode → UTC
    assert activation.input_to_utc_iso("2026-07-01T12:00", "local") == "2026-07-01T10:00"
    assert activation.input_to_utc_iso("2026-07-01T12:00", "utc") == "2026-07-01T12:00"
    # Contact (qso_date/time_on UTC) affiché en local
    d = activation.contact_disp("20260701", "1000", "local")
    assert d.strftime("%H:%M") == "12:00"


def test_past_slots() -> None:
    now = datetime.now(timezone.utc)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    past = activation.add_slot("F4IOZ", fmt(now - timedelta(hours=3)), fmt(now - timedelta(hours=1)), "20M", "SSB")
    cur = activation.add_slot("F5RRO", fmt(now - timedelta(hours=1)), fmt(now + timedelta(hours=1)), "40M", "CW")
    fut = activation.add_slot("F6ABC", fmt(now + timedelta(hours=1)), fmt(now + timedelta(hours=2)), "10M", "FM")
    ids = {s["id"] for s in activation.past_slots()}
    assert past in ids
    assert cur not in ids and fut not in ids


def test_tz_route_sets_cookie() -> None:
    client = TestClient(app, follow_redirects=False)
    r = client.get("/activation/tz?mode=utc&next=/activation")
    assert r.status_code == 303
    assert "tm_tz=utc" in r.headers.get("set-cookie", "")
    # valeur invalide → retombe sur local, next non interne → /activation
    r2 = client.get("/activation/tz?mode=bogus&next=https://evil/")
    assert "tm_tz=local" in r2.headers.get("set-cookie", "")
    assert r2.headers["location"] == "/activation"
    # « /\hote » : certains navigateurs le lisent comme « //hote » (externe)
    for evil in ("//evil.example/", "/%5Cevil.example/"):
        assert client.get(f"/activation/tz?mode=utc&next={evil}").headers["location"] == "/activation"
    # page publique d'un indicatif : chemin interne accepté
    assert client.get("/activation/tz?mode=utc&next=/tm61xyz").headers["location"] == "/tm61xyz"


def test_to_adif_roundtrips() -> None:
    activation.add_contact(
        call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ",
        qso_date="20260907", time_on="1015", rst_sent="59", rst_rcvd="57",
        gridsquare="JO31", freq_mhz=14.19,
    )
    adif = activation.to_adif()
    assert "<STATION_CALLSIGN:8>TM25TEST" in adif
    assert "<EOR>" in adif
    recs = parse_adif(adif)
    assert len(recs) == 1
    assert recs[0]["call"].upper() == "DL1ABC"
    assert recs[0]["station_callsign"].upper() == "TM25TEST"
    assert recs[0]["operator"].upper() == "F4IOZ"


def test_to_csv_has_header_and_rows() -> None:
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    csv_text = activation.to_csv()
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    assert lines[0].startswith("qso_date,time_on,call")
    assert any("DL1ABC" in ln for ln in lines[1:])


# ── Routes / auth ──────────────────────────────────────────────────────────


def _private_client() -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(auth_mod.COOKIE_NAME, auth_mod.make_token())
    return client


def test_routes_require_auth() -> None:
    client = TestClient(app, follow_redirects=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        for path in ("/activation", "/activation/planning", "/activation/log"):
            r = client.get(path)
            assert r.status_code == 303
            assert r.headers["location"].startswith("/activation/login")


def test_dashboard_ok_in_private_mode() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        r = client.get("/activation")
        assert r.status_code == 200
        assert "TM25TEST" in r.text


def test_export_endpoints() -> None:
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        radif = client.get("/activation/export.adi")
        assert radif.status_code == 200
        assert "attachment" in radif.headers.get("content-disposition", "")
        assert "<EOR>" in radif.text
        rcsv = client.get("/activation/export.csv")
        assert rcsv.status_code == 200
        assert "DL1ABC" in rcsv.text


def test_import_adif_adds_and_dedupes() -> None:
    adif = (
        "<CALL:5>DL1XX <BAND:3>20M <MODE:3>SSB <QSO_DATE:8>20260907 <TIME_ON:4>1015 <EOR>\n"
        "<CALL:5>DL2XX <BAND:3>40M <MODE:2>CW <QSO_DATE:8>20260907 <TIME_ON:6>101700 <EOR>\n"
        "<CALL:3>!!! <BAND:3>20M <MODE:3>SSB <QSO_DATE:8>20260907 <TIME_ON:4>1015 <EOR>\n"  # call invalide
        "<CALL:5>DL3XX <BAND:3>20M <MODE:3>SSB <EOR>\n"  # sans date/heure
    )
    res = activation.import_adif(adif, operator_call="F4IOZ")
    assert res["added"] == 2
    assert res["invalid"] == 2
    # Ré-import : QSO déjà au log ignorés
    res2 = activation.import_adif(adif, operator_call="F4IOZ")
    assert res2["added"] == 0
    assert res2["skipped"] == 2


def test_import_uses_adif_operator_field() -> None:
    adif = "<CALL:5>DL3XX <BAND:3>20M <MODE:3>SSB <QSO_DATE:8>20260907 <TIME_ON:4>1015 <OPERATOR:5>F6ABC <EOR>"
    activation.import_adif(adif, operator_call="F4IOZ")
    ops = {c["operator_call"] for c in activation.list_contacts()}
    assert "F6ABC" in ops  # champ ADIF prioritaire sur le défaut (import direct)


def test_public_board_404_when_disabled() -> None:
    client = TestClient(app)
    assert client.get("/tm25test").status_code == 404


def test_public_board_ok_when_enabled(monkeypatch) -> None:
    _public()
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    activation.set_flag("show_contacts", True)  # rendre la liste visible pour ce test
    client = TestClient(app)
    r = client.get("/tm25test")
    assert r.status_code == 200
    assert "TM25TEST" in r.text
    assert "DL1ABC" in r.text


def test_import_preview_then_confirm_selected_only() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        r, token = _import_preview(client, _adif(_rec_adif("G0ABC"), _rec_adif("G0DEF", band="40M")))
        assert "Aperçu" in r.text and "G0DEF" in r.text
        assert activation.stats()["total"] == 0          # rien avant confirmation
        r2 = client.post("/activation/import/confirm", data={
            "token": token, "operator": "F4IOZ", "op_source": "form", "sel": ["0"]})
        assert r2.status_code == 303 and "imported=1" in r2.headers["location"]
        assert [c["call"] for c in activation.list_contacts()] == ["G0ABC"]
        # Jeton consommé : une 2e confirmation n'importe rien
        r3 = client.post("/activation/import/confirm", data={
            "token": token, "operator": "F4IOZ", "op_source": "form", "sel": ["0", "1"]})
        assert "err=expired" in r3.headers["location"]
        assert activation.stats()["total"] == 1


def test_import_preview_attaches_operator_from_file_or_form() -> None:
    text = _adif(_rec_adif("G0ABC", operator="F5RRO"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        _, token = _import_preview(client, text, operator="F4IOZ", op_source="file")
        client.post("/activation/import/confirm", data={
            "token": token, "operator": "F4IOZ", "op_source": "file", "sel": ["0"]})
    assert activation.list_contacts()[0]["operator_call"] == "F5RRO"


def test_log_contact_via_htmx() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        r = client.post(
            "/activation/contacts",
            data={"call": "G0ABC", "band": "40M", "mode": "CW", "operator": "F4IOZ"},
        )
        assert r.status_code == 200
        assert "G0ABC" in r.text
        assert activation.stats()["total"] == 1


def test_update_contact() -> None:
    cid = activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    activation.update_contact(cid, call="DL9ZZ", band="40M", sat_name="so-50")
    c = activation.get_contact(cid)
    assert c["call"] == "DL9ZZ"
    assert c["band"] == "40M"
    assert c["sat_name"] == "SO-50"


def test_update_contact_rejects_bad_call() -> None:
    cid = activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    with pytest.raises(ValueError):
        activation.update_contact(cid, call="!!!")


def test_sat_qso_adif_prop_mode() -> None:
    activation.add_contact(
        call="DL1ABC", band="2M", mode="SSB", operator_call="F4IOZ", sat_name="SO-50",
    )
    adif = activation.to_adif()
    assert "<SAT_NAME:5>SO-50" in adif
    assert "<PROP_MODE:3>SAT" in adif


def test_edit_contact_route() -> None:
    cid = activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        assert client.get(f"/activation/contacts/{cid}/edit").status_code == 200
        r = client.post(
            f"/activation/contacts/{cid}",
            data={"call": "DL9ZZ", "band": "40M", "mode": "CW", "operator": "F4IOZ",
                  "qso_date": "20260907", "time_on": "1200", "sat_name": "RS-44"},
        )
        assert r.status_code == 303
    c = activation.get_contact(cid)
    assert c["call"] == "DL9ZZ" and c["sat_name"] == "RS-44"


def test_set_operator_password_persists(tmp_path, monkeypatch) -> None:
    # OP_PASSWORD_FILE déjà patché par le fixture ; on vérifie l'écriture + lecture.
    activation.set_operator_password("club2026!")
    assert activation.operator_password() == "club2026!"


def test_operator_token_domain_separation() -> None:
    """Un jeton opérateur ne doit JAMAIS être accepté comme jeton admin site."""
    tok = activation.make_op_token()
    assert activation.verify_op_token(tok)
    assert not auth_mod.verify_token(tok)  # signé "activation:{ts}", pas "{ts}"
    # Inversement, un jeton admin n'ouvre pas la session opérateur.
    admin_tok = auth_mod.make_token()
    assert not activation.verify_op_token(admin_tok)


def test_operator_login_registers_call_and_grants_access() -> None:
    activation.set_operator_password("oppass")
    client = TestClient(app, follow_redirects=False)
    # Sans session → redirection vers le login opérateur
    assert client.get("/activation").headers["location"].startswith("/activation/login")
    # Indicatif manquant → 401
    assert client.post("/activation/login", data={"password": "oppass"}).status_code == 401
    # Mauvais mot de passe → 401
    assert client.post("/activation/login", data={"callsign": "F5TEST", "password": "nope"}).status_code == 401
    # Indicatif + bon mot de passe → 303, cookies posés, indicatif enrôlé
    r = client.post("/activation/login", data={"callsign": "f5test", "password": "oppass", "next": "/activation/log"})
    assert r.status_code == 303
    sc = r.headers.get("set-cookie", "")
    assert activation.OP_COOKIE in sc and "tm_op" in sc
    assert "F5TEST" in {o["callsign"] for o in activation.list_operators()}
    # Session opérateur → accès à l'espace
    assert client.get("/activation").status_code == 200


def test_settings_reserved_to_admin() -> None:
    activation.set_operator_password("oppass")
    client = TestClient(app, follow_redirects=False)
    client.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    # Opérateur : accès au log oui, aux réglages non (→ /login admin)
    assert client.get("/activation/log").status_code == 200
    assert client.get("/activation/settings").headers["location"].startswith("/login")
    assert client.post(
        "/activation/settings/password", data={"new_password": "x", "confirm": "x"}
    ).headers["location"].startswith("/login")


def test_admin_bypass_without_operator_password() -> None:
    """Sans mot de passe opérateur configuré, l'admin site accède quand même."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        assert client.get("/activation").status_code == 200


def test_update_slot() -> None:
    sid = activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    activation.update_slot(sid, "F6ABC", "2026-09-07T11:00", "2026-09-07T13:00", "40M", "CW", "note")
    s = activation.get_slot(sid)
    assert s["operator_call"] == "F6ABC" and s["band"] == "40M" and s["mode"] == "CW"


def test_conflicting_slot_ids() -> None:
    a = activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    b = activation.add_slot("F6ABC", "2026-09-07T11:00", "2026-09-07T13:00", "20M", "CW")  # overlap same band
    c = activation.add_slot("F4IOZ", "2026-09-07T11:00", "2026-09-07T13:00", "40M", "CW")  # autre bande
    bad = activation.conflicting_slot_ids()
    assert a in bad and b in bad
    assert c not in bad


def test_future_slots_excludes_current() -> None:
    now = datetime.now(timezone.utc)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    cur = activation.add_slot("F4IOZ", fmt(now - timedelta(hours=1)), fmt(now + timedelta(hours=1)), "20M", "SSB")
    fut = activation.add_slot("F6ABC", fmt(now + timedelta(hours=2)), fmt(now + timedelta(hours=3)), "40M", "CW")
    ids = {s["id"] for s in activation.future_slots()}
    assert fut in ids
    assert cur not in ids  # l'activation en cours n'est PAS dans « à venir »
    current, _ = activation.current_and_next_slot()
    assert current and current["id"] == cur


def test_live_slots_handles_multiple_simultaneous() -> None:
    now = datetime.now(timezone.utc)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    a = activation.add_slot("F4IOZ", fmt(now - timedelta(hours=2)), fmt(now + timedelta(hours=2)), "2M", "SSB")
    b = activation.add_slot("F5RRO", fmt(now - timedelta(hours=1)), fmt(now + timedelta(hours=3)), "20M", "SSB")
    c = activation.add_slot("F6ABC", fmt(now + timedelta(hours=5)), fmt(now + timedelta(hours=6)), "10M", "CW")
    live = {s["id"] for s in activation.live_slots()}
    fut = {s["id"] for s in activation.future_slots()}
    assert a in live and b in live  # DEUX activations en cours affichées
    assert c not in live
    assert c in fut and a not in fut and b not in fut
    # ensemble : tout créneau non terminé est couvert (en cours ∪ à venir)
    assert {a, b, c} <= (live | fut)


def test_contacts_for_call() -> None:
    activation.add_contact(call="F1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    activation.add_contact(call="F1ABC", band="40M", mode="CW", operator_call="F4IOZ")
    res = activation.contacts_for_call("f1abc")
    assert res["found"] and res["total"] == 2 and res["band_modes"] == 2
    assert not activation.contacts_for_call("!!bad")["found"]


def test_edit_slot_route() -> None:
    sid = activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        assert client.get(f"/activation/slots/{sid}/edit").status_code == 200
        r = client.post(
            f"/activation/slots/{sid}",
            data={"operator": "F6ABC", "start": "2026-09-07T13:00", "end": "2026-09-07T14:00",
                  "band": "40M", "mode": "CW", "note": ""},
        )
        assert r.status_code == 303
    assert activation.get_slot(sid)["operator_call"] == "F6ABC"


def test_public_search_route(monkeypatch) -> None:
    _public()
    activation.add_contact(call="F1ABC", band="20M", mode="SSB", operator_call="F5RRO")
    client = TestClient(app)
    r = client.get("/tm25test?call=F1ABC")
    assert r.status_code == 200
    assert "F1ABC" in r.text           # indicatif recherché affiché
    assert "Opérateur" in r.text        # colonne opérateur
    assert "F5RRO" in r.text            # opérateur TM25TEST qui l'a contacté


def test_show_contacts_flag_default_off_and_toggle() -> None:
    assert activation.show_contacts() is False
    activation.set_flag("show_contacts", True)
    assert activation.show_contacts() is True
    activation.set_flag("show_contacts", False)
    assert activation.show_contacts() is False


def test_public_hides_contacts_unless_enabled(monkeypatch) -> None:
    _public()
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    client = TestClient(app)
    # Par défaut : liste des contacts masquée
    t = client.get("/tm25test").text
    assert "Derniers contacts" not in t
    # Activée → visible
    activation.set_flag("show_contacts", True)
    t2 = client.get("/tm25test").text
    assert "Derniers contacts" in t2 and "DL1ABC" in t2


def test_change_flags_route_admin_only() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        admin = _private_client()
        r = admin.post("/activation/settings/flags", data={"show_contacts": "1"})
        assert r.status_code == 303
        assert activation.show_contacts() is True
    # opérateur non-admin → redirigé /login
    activation.set_operator_password("oppass")
    op = TestClient(app, follow_redirects=False)
    op.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    assert op.post("/activation/settings/flags", data={"show_contacts": "1"}).headers["location"].startswith("/login")


def test_utc_iso_to_parts() -> None:
    assert activation.utc_iso_to_parts("2026-07-01T10:05") == ("20260701", "1005")
    assert activation.utc_iso_to_parts("bad") is None


def test_log_contact_with_explicit_datetime() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        # Mode local (défaut) : 12:00 Paris (été) → 10:00 UTC stocké
        r = client.post("/activation/contacts", data={
            "call": "G0ABC", "band": "20M", "mode": "SSB", "operator": "F4IOZ",
            "when": "2026-07-01T12:00",
        })
        assert r.status_code == 200
    cs = activation.list_contacts()
    assert any(c["qso_date"] == "20260701" and c["time_on"] == "1000" for c in cs)


def test_backup_now_creates_snapshot() -> None:
    import sqlite3
    activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    p = activation.backup_now()
    assert p.exists()
    assert activation.list_backups()
    c = sqlite3.connect(p)
    try:
        assert c.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 1
    finally:
        c.close()


def test_backup_includes_password() -> None:
    activation.set_operator_password("clubsecret")  # déclenche backup_now()
    pw = list(activation.BACKUP_DIR.glob("password-*.txt"))
    assert pw
    assert pw[0].read_text() == "clubsecret"


def test_rotate_keeps_last_n(monkeypatch) -> None:
    monkeypatch.setattr(activation, "BACKUP_KEEP", 2)
    activation.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for n in ("20260101-000001", "20260101-000002", "20260101-000003"):
        (activation.BACKUP_DIR / f"activation-{n}.sqlite").write_text("x")
    activation._rotate_backups()
    remaining = sorted(p.name for p in activation.BACKUP_DIR.glob("activation-*.sqlite"))
    assert remaining == ["activation-20260101-000002.sqlite", "activation-20260101-000003.sqlite"]


def test_maybe_backup_throttles() -> None:
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    n1 = len(activation.list_backups())
    activation.add_contact(call="DL2ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    n2 = len(activation.list_backups())
    assert n2 == n1  # 2e écriture dans la fenêtre de throttle → pas de nouveau snapshot


def test_backup_download_admin_only() -> None:
    activation.set_operator_password("oppass")
    client = TestClient(app, follow_redirects=False)
    client.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    # opérateur (pas admin) → redirigé vers /login
    assert client.get("/activation/settings/backup.sqlite").headers["location"].startswith("/login")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        admin = _private_client()
        r = admin.get("/activation/settings/backup.sqlite")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")


def test_change_operator_password_route(monkeypatch) -> None:
    # Accès via admin site (bypass) pour l'amorçage
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    client = _private_client()
    r = client.post("/activation/settings/password",
                    data={"new_password": "a", "confirm": "b"})
    assert "pw=mismatch" in r.headers["location"]
    r = client.post("/activation/settings/password",
                    data={"new_password": "club2026!", "confirm": "club2026!"})
    assert "pw=ok" in r.headers["location"]
    assert activation.operator_password() == "club2026!"


# ── Callbook QRZ / carte / DXCC / classement ───────────────────────────────


class _FakeQrz:
    """Client QRZ XML simulé : fiches fixes, compte les interrogations."""

    def __init__(self, records: dict | None = None, error: bool = False) -> None:
        self.records = records or {}
        self.error = error
        self.asked: list[str] = []

    def lookup_with_status(self, cs: str):
        self.asked.append(cs)
        if self.error:
            return None, "error"
        rec = self.records.get(cs)
        return (rec, "ok") if rec else (None, "notfound")


def _rec(call: str, grid: str = "JO31", land: str = "Germany", dxcc: str = "230") -> XmlLookup:
    return XmlLookup(call=call, name="Hans Muster", fname="Hans", lname="Muster",
                     grid=grid, country=land, land=land, dxcc=dxcc, cqzone="14")


def _age_callbook(call: str, seconds: int) -> None:
    with activation.conn() as c:
        c.execute("UPDATE callbook SET fetched_at = fetched_at - ? WHERE call = ?", (seconds, call))


def _qso(call: str, band: str = "20M", mode: str = "SSB", time_on: str | None = None, grid: str = "") -> None:
    activation.add_contact(
        call=call, band=band, mode=mode, operator_call="F4IOZ",
        qso_date="20260910" if time_on else None, time_on=time_on, gridsquare=grid,
    )


def test_locator_center() -> None:
    lat, lon = activation.locator_center("JN18FS")
    assert 48.75 < lat < 48.80 and 2.41 < lon < 2.50
    assert activation.locator_center("JN18") == (48.5, 3.0)
    assert activation.locator_center("bad") is None


def test_qrz_lookup_caches_in_callbook() -> None:
    fake = _FakeQrz({"DL1ABC": _rec("DL1ABC", grid="JO31ab")})
    row = activation.qrz_lookup("dl1abc", fake)
    assert row["fname"] == "Hans" and row["name"] == "Muster"
    assert row["grid"] == "JO31AB" and row["dxcc"] == 230 and row["dxcc_name"] == "Germany"
    assert activation.qrz_lookup("DL1ABC", fake)["grid"] == "JO31AB"
    assert fake.asked == ["DL1ABC"]  # 2e appel servi par le callbook


def test_qrz_lookup_notfound_retried_after_24h() -> None:
    fake = _FakeQrz()
    assert activation.qrz_lookup("DL9XYZ", fake) is None
    assert activation.qrz_lookup("DL9XYZ", fake) is None
    assert fake.asked == ["DL9XYZ"]
    _age_callbook("DL9XYZ", activation.CALLBOOK_RETRY_NOTFOUND + 1)
    activation.qrz_lookup("DL9XYZ", fake)
    assert fake.asked == ["DL9XYZ", "DL9XYZ"]


def test_qrz_lookup_portable_falls_back_to_base_call() -> None:
    fake = _FakeQrz({"DL1ABC": _rec("DL1ABC")})
    row = activation.qrz_lookup("DL1ABC/P", fake)
    assert row is not None and row["call"] == "DL1ABC/P"
    assert fake.asked == ["DL1ABC/P", "DL1ABC"]


def test_enrich_one_walks_pending_calls() -> None:
    for call in ("DL1ABC", "G0XYZ", "DL1ABC"):
        _qso(call)
    fake = _FakeQrz({"DL1ABC": _rec("DL1ABC")})
    assert activation.enrich_one(fake) == "ok"
    assert activation.enrich_one(fake) == "notfound"   # G0XYZ inconnu de QRZ
    assert activation.enrich_one(fake) is None         # plus rien en attente
    assert fake.asked == ["DL1ABC", "G0XYZ"]
    assert activation.callbook_progress() == {"stations": 2, "ok": 1, "notfound": 1, "pending": 0}


def test_enrich_error_is_retried_sooner() -> None:
    _qso("DL1ABC")
    assert activation.enrich_one(_FakeQrz(error=True)) == "error"
    assert activation.pending_callbook_calls() == []
    _age_callbook("DL1ABC", activation.CALLBOOK_RETRY_ERROR + 1)
    assert activation.pending_callbook_calls() == ["DL1ABC"]
    assert activation.enrich_one(None) is None  # sans compte QRZ : rien


def test_map_data_prefers_logged_grid_and_hides_names() -> None:
    _qso("DL1ABC", grid="JO31AB")
    _qso("DL2ABC", grid="JO31AB")
    _qso("G0XYZ")
    _qso("F1ZZZ")
    _qso("ON4ZZ", grid="JO20")
    fake = _FakeQrz({"DL1ABC": _rec("DL1ABC", grid="JO40"),
                     "G0XYZ": _rec("G0XYZ", grid="IO91", land="England", dxcc="223"),
                     "ON4ZZ": _rec("ON4ZZ", grid="JO20CD", land="Belgium", dxcc="209")})
    for call in ("DL1ABC", "G0XYZ", "F1ZZZ", "ON4ZZ"):
        activation.qrz_lookup(call, fake)
    data = activation.map_data()
    assert data["stations"] == 5 and data["located"] == 4
    by_grid = {p["grid"]: p["calls"] for p in data["points"]}
    assert by_grid == {
        "JO31AB": ["DL1ABC", "DL2ABC"],  # locator saisi > QRZ (autre carré)
        "IO91": ["G0XYZ"],               # rien de saisi → QRZ
        "JO20CD": ["ON4ZZ"],             # saisi à 4 car., QRZ précise le même carré
    }
    dumped = json.dumps(data)
    assert "Hans" not in dumped and "Muster" not in dumped


def test_dxcc_table_counts_entities() -> None:
    for call in ("DL1ABC", "DL2ABC", "DL1ABC", "G0XYZ", "F1ZZZ"):
        _qso(call)
    fake = _FakeQrz({"DL1ABC": _rec("DL1ABC"), "DL2ABC": _rec("DL2ABC"),
                     "G0XYZ": _rec("G0XYZ", land="England", dxcc="223")})
    for call in ("DL1ABC", "DL2ABC", "G0XYZ", "F1ZZZ"):
        activation.qrz_lookup(call, fake)
    t = activation.dxcc_table()
    assert t["count"] == 2 and t["unidentified"] == 1
    assert t["entities"][0] == {"dxcc": 230, "dxcc_name": "Germany", "stations": 2, "qsos": 3}


def test_hunters_ranking_order() -> None:
    _qso("DL1ABC", time_on="1000")
    _qso("DL1ABC", time_on="1010")                  # 1 bande×mode, 2 QSO
    _qso("G0XYZ", time_on="1100")
    _qso("G0XYZ", band="40M", mode="CW", time_on="1110")  # 2 bande×mode
    _qso("F1ZZZ", time_on="0900")                   # 1 bande×mode, 1 QSO
    _qso("F2ZZZ", time_on="0800")                   # ex aequo avec F1ZZZ, mais plus tôt
    r = activation.hunters_ranking()
    assert [h["call"] for h in r] == ["G0XYZ", "DL1ABC", "F2ZZZ", "F1ZZZ"]
    assert [h["rank"] for h in r] == [1, 2, 3, 4]
    assert r[0]["band_modes"] == 2 and r[1]["qsos"] == 2


def test_log_contact_now_ignores_typed_datetime() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        client = _private_client()
        r = client.post("/activation/contacts", data={
            "call": "G0ABC", "band": "20M", "mode": "SSB", "operator": "F4IOZ",
            "when": "2020-01-01T12:00", "now": "1",
        })
        assert r.status_code == 200
    q = activation.list_contacts()[0]
    assert q["qso_date"] == datetime.now(timezone.utc).strftime("%Y%m%d")


def test_qrz_route_auth_and_json(monkeypatch) -> None:
    fake = _FakeQrz({"DL1ABC": _rec("DL1ABC", grid="JO31AB")})
    monkeypatch.setattr(activation, "qrz_client", lambda: fake)
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/activation/qrz?call=DL1ABC").status_code == 303
    client = _private_client()
    d = client.get("/activation/qrz?call=dl1abc").json()
    assert d == {"call": "DL1ABC", "found": True, "fname": "Hans", "name": "Muster",
                 "grid": "JO31AB", "country": "Germany"}
    d = client.get("/activation/qrz?call=ZZ9ZZZ").json()
    assert d["found"] is False and d["configured"] is True
    assert client.get("/activation/qrz?call=!!").status_code == 400


def test_public_board_map_dxcc_ranking(monkeypatch) -> None:
    _public()
    _qso("DL1ABC", grid="JO31AB")
    activation.qrz_lookup("DL1ABC", _FakeQrz({"DL1ABC": _rec("DL1ABC")}))
    client = TestClient(app)
    t = client.get("/tm25test").text
    assert 'id="act-map"' in t and "JO31AB" in t and "Classement des chasseurs" in t
    assert "Germany" in t and "DL1ABC" in t
    assert "Hans" not in t and "Muster" not in t     # jamais de nom en public
    activation.set_flag("show_map_stats", False)
    t2 = client.get("/tm25test").text
    assert 'id="act-map"' not in t2 and "DL1ABC" not in t2


def test_change_flags_route_sets_map_flag() -> None:
    assert activation.show_map_stats() is True   # défaut : visible
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        admin = _private_client()
        admin.post("/activation/settings/flags", data={"show_contacts": "1"})
        assert activation.show_map_stats() is False
        admin.post("/activation/settings/flags", data={"show_map_stats": "1"})
        assert activation.show_map_stats() is True and activation.show_contacts() is False


def test_locator_center_8_chars() -> None:
    lat, lon = activation.locator_center("JN18FS89")   # TM25TEST, Villeneuve
    assert abs(lat - 48.7896) < 0.001 and abs(lon - 2.4875) < 0.001


_RULE = {
    "enabled": "1", "unique_band_mode": "1",
    "per_qso_on": "1", "per_qso": "1",
    "mode_on": "1", "mode_CW": "3", "mode_SSB": "2", "mode_default": "1",
    "distance_on": "1", "km_per_point": "1000", "distance_max": "0",
}


def test_scoring_defaults_and_save_clamps() -> None:
    rule = activation.get_scoring()
    assert rule["enabled"] is False and rule["mode_points"]["CW"] == 3
    saved = activation.set_scoring({"enabled": "1", "per_qso_on": "1", "per_qso": "9999",
                                    "mode_on": "1", "mode_CW": "5", "km_per_point": "0"})
    assert saved["per_qso"] == 1000 and saved["mode_points"]["CW"] == 5 and saved["km_per_point"] == 1
    rule = activation.get_scoring()
    assert rule["enabled"] is True and rule["distance_on"] is False
    assert rule["mode_points"]["SSB"] == 2    # champ absent → défaut


def test_hunters_ranking_with_points() -> None:
    activation.set_scoring(_RULE)
    _qso("VK2ABC", grid="QF56")              # ~16 900 km, SSB : 1 + 2 + 16
    _qso("DL1ABC", mode="CW", grid="JO31")   # < 1000 km, CW : 1 + 3
    _qso("DL1ABC", mode="CW", grid="JO31")   # doublon bande×mode : 0
    _qso("G0XYZ")                            # locator inconnu : 1 + 2
    r = {h["call"]: h for h in activation.hunters_ranking()}
    assert r["VK2ABC"]["points"] == 19 and r["VK2ABC"]["km"] > 16000
    assert r["DL1ABC"]["points"] == 4
    assert r["G0XYZ"]["points"] == 3 and r["G0XYZ"]["km"] is None
    assert [h["call"] for h in activation.hunters_ranking()] == ["VK2ABC", "DL1ABC", "G0XYZ"]
    activation.set_scoring({**_RULE, "unique_band_mode": ""})
    assert {h["call"]: h["points"] for h in activation.hunters_ranking()}["DL1ABC"] == 8
    activation.set_scoring({**_RULE, "distance_max": "10"})
    assert {h["call"]: h["points"] for h in activation.hunters_ranking()}["VK2ABC"] == 13


def test_scoring_settings_route_admin_only(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    admin = _private_client()
    r = admin.post("/activation/settings/scoring", data={"enabled": "1", "per_qso_on": "1", "per_qso": "2"})
    assert r.status_code == 303 and "sc=ok" in r.headers["location"]
    assert activation.get_scoring()["per_qso"] == 2
    page = admin.get("/activation/settings").text
    assert "Classement aux points" in page and 'name="mode_CW"' in page
    activation.set_operator_password("oppass")
    op = TestClient(app, follow_redirects=False)
    op.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    assert op.post("/activation/settings/scoring", data={"enabled": "1"}).headers["location"].startswith("/login")


def test_public_board_shows_points_when_enabled(monkeypatch) -> None:
    _public("JN18FS89")
    _qso("DL1ABC", mode="CW", grid="JO31")
    client = TestClient(app)
    assert "<th>Points</th>" not in client.get("/tm25test").text
    activation.set_scoring({"enabled": "1", "per_qso_on": "1", "per_qso": "1", "mode_on": "1", "mode_CW": "3"})
    t = client.get("/tm25test?call=DL1ABC").text
    assert "<th>Points</th>" in t and "<b>4</b>" in t   # 1 (contact) + 3 (CW)
    assert "1 pt par QSO" in t and "classement" in t


def test_call_grids_prefers_qrz_when_logged_locator_is_far() -> None:
    _qso("RA1AVP", grid="FN14NX")   # Canada saisi pour une station de Saint-Pétersbourg
    _qso("F5AYZ", grid="JN18EU")    # à ~40 km de QRZ : la saisie est gardée
    fake = _FakeQrz({"RA1AVP": _rec("RA1AVP", grid="KP50EA", land="Russia", dxcc="54"),
                     "F5AYZ": _rec("F5AYZ", grid="JN18HM", land="France", dxcc="227")})
    for call in ("RA1AVP", "F5AYZ"):
        activation.qrz_lookup(call, fake)
    assert activation.call_grids() == {"F5AYZ": "JN18EU", "RA1AVP": "KP50EA"}
    # Le log lui-même n'est pas modifié (export ADIF = ce qui a été saisi)
    assert {c["call"]: c["gridsquare"] for c in activation.list_contacts()}["RA1AVP"] == "FN14NX"


def test_map_data_distrusts_aa_locators() -> None:
    _qso("F1ACK", grid="JN16AA")   # « AA » inventé par un logiciel de log
    _qso("F1HOM", grid="JN16AA")   # idem, mais pas (encore) de fiche QRZ
    activation.qrz_lookup("F1ACK", _FakeQrz({"F1ACK": _rec("F1ACK", grid="JN18EU", land="France", dxcc="227")}))
    by_grid = {p["grid"]: p["calls"] for p in activation.map_data()["points"]}
    assert by_grid == {"JN18EU": ["F1ACK"], "JN16AA": ["F1HOM"]}


# ── Import / export ADIF ───────────────────────────────────────────────────


def _f(name: str, value: str) -> str:
    return f"<{name}:{len(value)}>{value}"


def _rec_adif(call: str, band: str = "20M", mode: str = "SSB",
              date: str = "20260910", time: str = "1000", **extra: str) -> str:
    parts = [_f("CALL", call), _f("BAND", band), _f("MODE", mode)]
    if date:
        parts.append(_f("QSO_DATE", date))
    if time:
        parts.append(_f("TIME_ON", time))
    parts += [_f(k.upper(), v) for k, v in extra.items()]
    return " ".join(parts)


def _adif(*records: str) -> str:
    return "<ADIF_VER:5>3.1.4 <EOH>\n" + "\n".join(r + " <EOR>" for r in records)


def _import_preview(client: TestClient, text: str, operator: str = "F4IOZ", op_source: str = "form"):
    r = client.post("/activation/import", data={"operator": operator, "op_source": op_source},
                    files={"file": ("log.adi", text.encode(), "text/plain")})
    assert r.status_code == 200
    token = re.search(r'name="token" value="([0-9a-f]{32})"', r.text).group(1)
    return r, token


def test_analyze_adif_statuses() -> None:
    _qso("DL1ABC", time_on="1000")                                # au log : 20260910 10:00
    rows = activation.analyze_adif(_adif(
        _rec_adif("DL1ABC", time="1005"),                         # même QSO à 5 min
        _rec_adif("DL1ABC", date="20260911"),                     # autre jour
        _rec_adif("G0XYZ", band="40M", mode="CW"),
        _rec_adif("G0XYZ", band="40M", mode="CW", time="1002"),   # 2 fois dans le fichier
        _rec_adif("OK1AB", mode="MFSK", submode="FT4"),           # FT4 en ADIF 3
        _rec_adif("OK2AB", date=""),                              # sans date
    ), operator_call="F4IOZ")
    assert [r["status"] for r in rows] == ["in_log", "worked", "new", "file_dupe", "new", "invalid"]
    assert rows[4]["mode"] == "FT4"
    assert all(r["operator_call"] == "F4IOZ" for r in rows)


def test_analyze_adif_operator_choice() -> None:
    text = _adif(_rec_adif("DL1ABC", operator="F5RRO"), _rec_adif("DL2ABC", operator="TM25TEST"))
    forced = activation.analyze_adif(text, operator_call="F4IOZ")
    assert [r["operator_call"] for r in forced] == ["F4IOZ", "F4IOZ"]
    from_file = activation.analyze_adif(text, operator_call="F4IOZ", prefer_file_operator=True)
    assert [r["operator_call"] for r in from_file] == ["F5RRO", "F4IOZ"]  # TM25TEST n'est pas un opérateur
    no_op = activation.analyze_adif(text, operator_call="")
    assert {r["status"] for r in no_op} == {"invalid"}


def test_export_selection() -> None:
    for call in ("DL1ABC", "G0XYZ", "F1ZZZ"):
        _qso(call)
    ids = {c["call"]: c["id"] for c in activation.list_contacts()}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        anon = TestClient(app, follow_redirects=False)
        r = anon.post("/activation/export-selection.adi", data={"ids": [str(ids["DL1ABC"])]})
        assert r.headers["location"].startswith("/activation/login")
        client = _private_client()
        r = client.post("/activation/export-selection.adi",
                        data={"ids": [str(ids["DL1ABC"]), str(ids["F1ZZZ"])]})
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert "DL1ABC" in r.text and "F1ZZZ" in r.text and "G0XYZ" not in r.text
        assert r.text.count("<EOR>") == 2
        empty = client.post("/activation/export-selection.adi", data={})
        assert empty.status_code == 303 and "err=empty" in empty.headers["location"]


def test_adif_page_lists_contacts_with_group_filters() -> None:
    _qso("DL1ABC")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        assert TestClient(app, follow_redirects=False).get("/activation/adif").status_code == 303
        r = _private_client().get("/activation/adif")
        assert r.status_code == 200
        assert "Exporter la sélection" in r.text and "DL1ABC" in r.text
        assert 'data-filter="op"' in r.text and 'data-filter="day"' in r.text


# ── Indicatifs spéciaux (multi-indicatif) ──────────────────────────────────


def test_first_station_seeded_from_config_with_hero() -> None:
    st = activation.current_station()
    assert st["callsign"] == "TM25TEST" and st["slug"] == "tm25test"
    assert st["badge"] == "50 ANS" and st["flags"] == "fr,be" and st["gridsquare"] == "JN18"
    assert activation.callsign() == "TM25TEST" and activation.my_gridsquare() == "JN18"


def test_migration_from_single_callsign_base(tmp_path, monkeypatch) -> None:
    import sqlite3
    old = tmp_path / "old.sqlite"
    c = sqlite3.connect(old)
    c.executescript(
        "CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER, "
        "operator_call TEXT NOT NULL, call TEXT NOT NULL, qso_date TEXT NOT NULL, "
        "time_on TEXT NOT NULL, band TEXT NOT NULL, mode TEXT NOT NULL, freq_mhz REAL, "
        "rst_sent TEXT DEFAULT '', rst_rcvd TEXT DEFAULT '', gridsquare TEXT DEFAULT '', "
        "comment TEXT DEFAULT '', created_at INTEGER);"
        "CREATE TABLE slots (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_call TEXT NOT NULL, "
        "start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, band TEXT NOT NULL, mode TEXT NOT NULL, "
        "note TEXT DEFAULT '', created_at INTEGER);"
        "INSERT INTO contacts(operator_call, call, qso_date, time_on, band, mode) "
        "VALUES ('F5RRO', 'DL1ABC', '20260910', '1000', '20M', 'SSB');"
        "INSERT INTO slots(operator_call, start_utc, end_utc, band, mode) "
        "VALUES ('F5RRO', '2026-09-10T10:00', '2026-09-10T12:00', '20M', 'SSB');"
    )
    c.commit()
    c.close()
    monkeypatch.setattr(activation, "DB_PATH", old)
    activation.init_db()
    qsos = activation.list_contacts()
    assert [q["call"] for q in qsos] == ["DL1ABC"] and qsos[0]["station"] == "TM25TEST"
    assert len(activation.list_slots()) == 1
    snaps = list(activation.BACKUP_DIR.glob("premigration-*.sqlite"))
    assert len(snaps) == 1                               # copie intacte avant migration
    s = sqlite3.connect(snaps[0])
    try:
        assert "station" not in {r[1] for r in s.execute("PRAGMA table_info(contacts)")}
        assert s.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1
    finally:
        s.close()


def test_stations_scope_log_planning_and_export() -> None:
    _qso("DL1ABC")
    activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    activation.create_station("tm61test", label="Test 2", gridsquare="JN18FS89", public="1")
    assert activation.callsign() == "TM25TEST"            # créer ne bascule pas
    activation.set_current_station("TM61TEST")
    assert activation.list_contacts() == [] and activation.list_slots() == []
    assert activation.stats()["total"] == 0
    _qso("G0XYZ")
    assert [q["call"] for q in activation.list_contacts()] == ["G0XYZ"]
    assert [q["call"] for q in activation.list_contacts(station="TM25TEST")] == ["DL1ABC"]
    adif = activation.to_adif()
    assert "<STATION_CALLSIGN:8>TM61TEST" in adif and "<MY_GRIDSQUARE:8>JN18FS89" in adif
    assert "DL1ABC" not in adif
    assert not activation.is_dupe("DL1ABC", "20M", "SSB")  # doublons jugés par indicatif
    activation.set_current_station("TM25TEST")
    assert activation.is_dupe("DL1ABC", "20M", "SSB")


def test_scoring_is_per_station() -> None:
    activation.create_station("TM61TEST")
    activation.set_scoring({"enabled": "1", "per_qso_on": "1", "per_qso": "5"})
    assert activation.get_scoring()["per_qso"] == 5
    assert activation.get_scoring("TM61TEST")["enabled"] is False


def test_create_station_validation() -> None:
    for bad in ("!!", "GRID"):                            # invalide / sans chiffre (masquerait /grid)
        with pytest.raises(ValueError):
            activation.create_station(bad)
    with pytest.raises(ValueError):
        activation.create_station("TM25TEST")             # déjà enregistré
    with pytest.raises(ValueError):
        activation.create_station("TM61A", gridsquare="ZZ99")
    with pytest.raises(ValueError):
        activation.create_station("TM61B", start_date="2026-09-20", end_date="2026-09-10")
    st = activation.create_station("TM61C", flags="it, xx ,FR")
    assert st["flags"] == "it,fr" and st["public"] == 0


def test_public_pages_per_station_and_list() -> None:
    _public()
    _qso("DL1ABC")
    activation.create_station("TM61TEST", label="Deuxième", public="1", badge="10 ANS")
    activation.create_station("TM62HIDE")                 # non publiée
    client = TestClient(app, follow_redirects=False)
    t = client.get("/tm25test").text
    assert "50 ANS" in t and "DL1ABC" in t and "Activation terminée" not in t
    t61 = client.get("/tm61test")
    assert t61.status_code == 200 and "10 ANS" in t61.text and "Activation terminée" in t61.text
    assert "DL1ABC" not in t61.text                       # le log de TM25TEST n'y est pas
    r = client.get("/TM61TEST?call=F1ABC")
    assert r.status_code == 301 and r.headers["location"] == "/tm61test?call=F1ABC"
    assert client.get("/tm62hide").status_code == 404
    assert client.get("/zz9zzz").status_code == 404
    lst = client.get("/activations").text
    assert "TM25TEST" in lst and "TM61TEST" in lst and "TM62HIDE" not in lst
    assert client.get("/robots.txt").status_code == 200   # la route générique ne masque rien


def test_station_admin_routes(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    admin = _private_client()
    r = admin.post("/activation/stations", data={
        "callsign": "tm61test", "label": "Test", "gridsquare": "jn18fs89", "public": "1"})
    assert r.status_code == 303 and "st=created" in r.headers["location"]
    assert activation.get_station("TM61TEST")["gridsquare"] == "JN18FS89"
    bad = admin.post("/activation/stations", data={"callsign": "TM61TEST"})
    assert bad.status_code == 400 and "déjà enregistré" in bad.text
    assert admin.get("/activation/stations/tm61test/edit").status_code == 200
    r = admin.post("/activation/stations/tm61test", data={"label": "Renommé", "gridsquare": "JN18"})
    assert r.status_code == 303
    st = activation.get_station("TM61TEST")
    assert st["label"] == "Renommé" and st["public"] == 0   # case décochée = masquée
    r = admin.post("/activation/stations/tm61test/current")
    assert r.status_code == 303 and activation.callsign() == "TM61TEST"
    assert "EN COURS" in admin.get("/activation/settings").text
    activation.set_operator_password("oppass")
    op = TestClient(app, follow_redirects=False)
    op.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    assert op.post("/activation/stations", data={"callsign": "TM63X"}).headers["location"].startswith("/login")


def test_station_status_upcoming_vs_archive() -> None:
    activation.create_station("TM61OLD", public="1", start_date="2020-01-01", end_date="2020-01-10")
    activation.create_station("TM61NEW", public="1", start_date="2099-01-01")
    status = {s["callsign"]: activation.station_status(s) for s in activation.list_stations()}
    assert status == {"TM25TEST": "current", "TM61OLD": "archive", "TM61NEW": "upcoming"}
    client = TestClient(app)
    t = client.get("/tm61new").text
    assert "Activation à venir" in t and "Activation terminée" not in t
    assert "À venir" in client.get("/activations").text


def test_delete_station_only_without_qso() -> None:
    activation.create_station("TM61EMPTY")
    activation.set_current_station("TM61EMPTY")
    activation.add_slot("F4IOZ", "2026-10-01T10:00", "2026-10-01T12:00", "20M", "SSB")
    with pytest.raises(ValueError):
        activation.delete_station("TM61EMPTY")           # indicatif en cours
    activation.set_current_station("TM25TEST")
    _qso("DL1ABC")                                       # QSO de TM25TEST
    with pytest.raises(ValueError):
        activation.delete_station("TM25TEST")            # en cours ET avec QSO
    activation.create_station("TM61LOG")
    activation.set_current_station("TM61LOG")
    _qso("G0XYZ")
    activation.set_current_station("TM25TEST")
    with pytest.raises(ValueError, match="a des QSO"):
        activation.delete_station("TM61LOG")             # fiche avec QSO conservée
    activation.set_scoring({"enabled": "1"}, station="TM61EMPTY")
    activation.delete_station("TM61EMPTY")
    assert activation.get_station("TM61EMPTY") is None
    assert activation.list_slots(station="TM61EMPTY") == []           # créneaux partis avec
    assert "TM61EMPTY" not in activation.load_settings().get("scoring_by_station", {})
    assert activation.get_station("TM61LOG") is not None
    assert [q["call"] for q in activation.list_contacts()] == ["DL1ABC"]
    assert activation.list_backups()                                  # sauvegarde avant suppression
    with pytest.raises(ValueError):
        activation.delete_station("TM61EMPTY")           # inconnu


def test_delete_station_route(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.create_station("TM61EMPTY")
    activation.create_station("TM61LOG")
    activation.set_current_station("TM61LOG")
    _qso("G0XYZ")
    activation.set_current_station("TM25TEST")
    admin = _private_client()
    page = admin.get("/activation/settings").text
    assert "/activation/stations/tm61empty/delete" in page       # bouton ✕ : fiche vide
    assert "/activation/stations/tm61log/delete" not in page     # pas de ✕ : a des QSO
    assert "/activation/stations/tm25test/delete" not in page    # pas de ✕ : en cours
    r = admin.post("/activation/stations/tm61log/delete")
    assert r.status_code == 400 and "a des QSO" in r.text
    r = admin.post("/activation/stations/tm61empty/delete")
    assert r.status_code == 303 and "st=deleted" in r.headers["location"]
    assert activation.get_station("TM61EMPTY") is None
    assert admin.post("/activation/stations/tm61empty/delete").status_code == 404
    activation.set_operator_password("oppass")
    op = TestClient(app, follow_redirects=False)
    op.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    assert op.post("/activation/stations/tm61log/delete").headers["location"].startswith("/login")


def test_slot_notes_shown_in_cards(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    activation.add_slot("F5RRO", fmt(now + timedelta(hours=2)), fmt(now + timedelta(hours=3)),
                        "2M", "SSB", "QRV Sat FO-29")                        # à venir
    activation.add_slot("F5JRN", fmt(now - timedelta(hours=3)), fmt(now - timedelta(hours=2)),
                        "40M", "CW", "Depuis le local du club")              # passée
    _public()
    public = TestClient(app).get("/tm25test").text
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    dashboard = _private_client().get("/activation").text
    sat = '<p class="act-next-note"><span class="act-sat-icon" title="Satellite">🛰️</span> QRV Sat FO-29</p>'
    for page in (public, dashboard):
        assert sat in page                                                    # note satellite : icône
        assert '<p class="act-next-note">Depuis le local du club</p>' in page  # sans icône


def test_note_is_sat() -> None:
    for note in ("SAT FO-29", "QRV Sat FO-29", "via satellite ISS", "2 sats LEO"):
        assert activation.note_is_sat(note), note
    for note in ("", None, "Samedi matin", "Saturne", "Depuis le local du club", "SATCOM"):
        assert not activation.note_is_sat(note), note


# ── Compte QRZ.com (Réglages) ──────────────────────────────────────────────


def test_qrz_account_from_settings_overrides_config(monkeypatch) -> None:
    site_client = object()
    monkeypatch.setattr(activation, "get_shared_client", lambda: site_client)
    monkeypatch.setattr(activation, "qrz_config", lambda: {"username": "F4XYZ", "password": "site"})
    assert activation.qrz_account() == {"username": "F4XYZ", "source": "config"}
    assert _REAL_QRZ_CLIENT() is site_client

    activation.set_qrz_account("F6ABC", "clubpw")
    assert activation.qrz_account() == {"username": "F6ABC", "source": "settings"}
    assert activation.QRZ_ACCOUNT_FILE.stat().st_mode & 0o777 == 0o600
    client = _REAL_QRZ_CLIENT()
    assert isinstance(client, QrzXmlClient) and (client.username, client.password) == ("F6ABC", "clubpw")
    assert _REAL_QRZ_CLIENT() is client                  # une seule session QRZ
    activation.set_qrz_account("F6ABC", "newpw")
    assert _REAL_QRZ_CLIENT().password == "newpw"        # nouveau mot de passe → nouveau client

    activation.clear_qrz_account()
    assert activation.qrz_account()["source"] == "config" and _REAL_QRZ_CLIENT() is site_client
    monkeypatch.setattr(activation, "qrz_config", lambda: {})
    assert activation.qrz_account() == {"username": "", "source": ""}


def test_qrz_account_never_in_backups(monkeypatch) -> None:
    activation.set_qrz_account("F6ABC", "clubpw")
    activation.backup_now()
    for f in activation.BACKUP_DIR.iterdir():
        assert b"clubpw" not in f.read_bytes(), f.name


def test_qrz_settings_route(monkeypatch) -> None:
    checked: list[tuple[str, str]] = []
    answer = {"value": ("ok", "Wed Jan 6 2027")}

    def fake_check(user: str, pwd: str) -> tuple[str, str]:
        checked.append((user, pwd))
        return answer["value"]

    monkeypatch.setattr(activation, "check_qrz_account", fake_check)
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    admin = _private_client()

    r = admin.post("/activation/settings/qrz", data={"username": "F6ABC", "password": "clubpw"})
    assert r.headers["location"].endswith("qz=ok#qrz")
    assert activation.qrz_account() == {"username": "F6ABC", "source": "settings"}
    page = admin.get("/activation/settings").text
    assert "F6ABC (saisi ici)" in page and "clubpw" not in page

    # Mot de passe laissé vide, même identifiant : l'ancien est repris.
    admin.post("/activation/settings/qrz", data={"username": "f6abc", "password": ""})
    assert checked[-1] == ("f6abc", "clubpw")
    # Autre identifiant sans mot de passe : refusé.
    r = admin.post("/activation/settings/qrz", data={"username": "F5NEW", "password": ""})
    assert r.headers["location"].endswith("qz=incomplete#qrz")

    answer["value"] = ("refused", "Username/password incorrect")
    r = admin.post("/activation/settings/qrz", data={"username": "F5BAD", "password": "x"})
    assert r.headers["location"].endswith("qz=refused#qrz")
    assert activation.qrz_account()["username"] == "f6abc"           # compte précédent gardé

    answer["value"] = ("ok", "non-subscriber")
    r = admin.post("/activation/settings/qrz", data={"username": "F5FREE", "password": "y"})
    assert r.headers["location"].endswith("qz=nosub#qrz")
    answer["value"] = ("error", "timeout")
    r = admin.post("/activation/settings/qrz", data={"username": "F5OFF", "password": "z"})
    assert r.headers["location"].endswith("qz=offline#qrz") and activation.qrz_account()["username"] == "F5OFF"

    r = admin.post("/activation/settings/qrz", data={"action": "clear"})
    assert r.headers["location"].endswith("qz=cleared#qrz") and not activation.QRZ_ACCOUNT_FILE.exists()

    # Opérateur non admin → /login, rien n'est changé.
    activation.set_operator_password("oppass")
    op = TestClient(app, follow_redirects=False)
    op.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    r = op.post("/activation/settings/qrz", data={"username": "F5HACK", "password": "x"})
    assert r.headers["location"].startswith("/login") and not activation.QRZ_ACCOUNT_FILE.exists()


_REAL_HTTPX_CLIENT = qrz_xml.httpx.Client


def _qrz_transport(monkeypatch, handler) -> None:
    """Réponses QRZ simulées : httpx.Client de qrz_xml branché sur un MockTransport."""
    mock = qrz_xml.httpx.MockTransport(handler)
    monkeypatch.setattr(qrz_xml.httpx, "Client", lambda **kw: _REAL_HTTPX_CLIENT(transport=mock, **kw))


def test_qrz_check_login(monkeypatch) -> None:
    import httpx

    ns = 'xmlns="http://xmldata.qrz.com"'
    _qrz_transport(monkeypatch, lambda req: httpx.Response(
        200, text=f"<QRZDatabase {ns}><Session><Key>k1</Key><SubExp>Wed Jan 6 2027</SubExp></Session></QRZDatabase>"))
    assert QrzXmlClient("F6ABC", "pw").check_login() == ("ok", "Wed Jan 6 2027")
    _qrz_transport(monkeypatch, lambda req: httpx.Response(
        200, text=f"<QRZDatabase {ns}><Session><Error>Username/password incorrect</Error></Session></QRZDatabase>"))
    assert QrzXmlClient("F6ABC", "bad").check_login() == ("refused", "Username/password incorrect")

    def down(req):
        raise httpx.ConnectError("injoignable")

    _qrz_transport(monkeypatch, down)
    assert QrzXmlClient("F6ABC", "pw").check_login()[0] == "error"


# ── Pages en anglais (i18n) ────────────────────────────────────────────────

# Mots français courants : leur présence sur une page anglaise trahit un texte
# oublié (hors données saisies : les données de test sont neutres).
_FRENCH_WORDS = re.compile(
    r"(?i)(?<![\w-])(les|des|du|une|avec|pour|dans|sur|aucun|aucune|créneaux?|opérateurs?|réglages|"
    r"indicatifs?|bandes?|chasseurs|prochaines|tableau|contacté|mettre|supprimer|enregistrer|"
    r"à|où|être|été|ou|et|le|la)(?![\w-])"
)


def _visible_text(html: str) -> str:
    import html as html_mod

    html = re.sub(r"(?s)<script\b.*?</script>|<style\b.*?</style>", " ", html)
    attrs = " ".join(re.findall(r'(?:title|placeholder|aria-label)="([^"]*)"', html))
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", html) + " " + attrs)


def test_pages_render_in_english(monkeypatch) -> None:
    from app.routers import activation as act_router
    from app.templating import templates

    club = {"callsign": "F6ABC", "name": "Radio Club Test", "city": "Testville", "website": ""}
    monkeypatch.setitem(templates.env.globals, "club", club)
    monkeypatch.setattr(act_router, "club_config", lambda: club)
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    _public(grid="JN18")
    activation.update_station("TM25TEST", subtitle="Town A · Town B")   # donnée saisie, pas l'interface
    activation.set_flag("show_contacts", True)
    activation.set_flag("show_map_stats", True)
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M"
    sid = activation.add_slot("F4IOZ", (now + timedelta(hours=1)).strftime(fmt),
                              (now + timedelta(hours=2)).strftime(fmt), "20M", "CW", "SAT FO-29")
    _qso("DL1ABC")
    cid = activation.list_contacts()[0]["id"]

    en = {"Accept-Language": "en-GB,en;q=0.9"}
    anon = TestClient(app, headers=en)
    admin = _private_client()
    admin.headers.update(en)
    pages = [anon.get(p) for p in ("/tm25test", "/tm25test?call=DL1ABC", "/tm25test?call=ZZ9ZZZ",
                                    "/activations", "/activation/login")]
    pages += [admin.get(p) for p in ("/activation", "/activation/planning", "/activation/log",
                                     "/activation/adif", "/activation/settings",
                                     "/activation/stations/tm25test/edit", f"/activation/slots/{sid}/edit",
                                     f"/activation/contacts/{cid}/edit")]
    for r in pages:
        assert r.status_code == 200, r.url
        assert '<html lang="en">' in r.text, r.url
        words = sorted({m.group(0) for m in _FRENCH_WORDS.finditer(_visible_text(r.text))})
        assert not words, f"{r.url} : mots français {words}"
    assert "Upcoming activations" in pages[0].text and "Your QSOs with TM25TEST" in pages[1].text
    assert 'window.ACT_I18N' in pages[0].text and "Starts in {t}" in pages[0].text


def test_language_switch_cookie_and_fallbacks() -> None:
    _public()
    client = TestClient(app, follow_redirects=False)
    assert '<html lang="fr">' in client.get("/tm25test").text          # sans en-tête : français
    r = client.get("/activation/lang/en?next=/tm25test%3Fcall%3DDL1ABC")
    assert r.status_code == 303 and r.headers["location"] == "/tm25test?call=DL1ABC"
    assert "lang=en" in r.headers["set-cookie"] and "Max-Age=31536000" in r.headers["set-cookie"]
    page = client.get("/tm25test", headers={"Accept-Language": "fr-FR"}).text
    assert '<html lang="en">' in page                                  # le cookie prime sur le navigateur
    assert 'href="/activation/lang/fr?next=/tm25test"' in page
    # Redirection externe refusée, langue inconnue ignorée.
    assert client.get("/activation/lang/fr?next=//evil.example").headers["location"] == "/activations"
    assert "set-cookie" not in client.get("/activation/lang/xx").headers
    # Messages d'erreur Python traduits (ValueError de la couche données).
    r = TestClient(app).post("/activation/login", data={"callsign": "!!", "password": "x"},
                             headers={"Accept-Language": "en"})
    assert "Invalid callsign" in r.text
