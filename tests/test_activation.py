"""Tests du module d'activation d'indicatif temporaire (TM25TEST)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
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
    # F1ZZZ est inconnu de QRZ, mais son préfixe suffit à l'identifier.
    assert t["count"] == 3 and t["unidentified"] == 0
    first = t["entities"][0]
    assert first["dxcc_name"] == "Germany" and first["code"] == "DE"
    assert (first["dxcc"], first["stations"], first["qsos"]) == (230, 2, 3)
    assert [e["dxcc_name"] for e in t["entities"][1:]] == ["England", "France"]


def test_dxcc_table_works_without_qrz() -> None:
    """Sans compte QRZ (callbook vide), l'entité vient du préfixe de l'indicatif."""
    for call in ("DL1ABC", "G0XYZ", "F1ZZZ", "VE1ZZ", "XYZZY9"):
        _qso(call)
    t = activation.dxcc_table()
    assert [(e["dxcc_name"], e["code"]) for e in t["entities"]] == [
        ("Canada", "CA"), ("England", "GB-ENG"), ("France", "FR"), ("Germany", "DE")]
    assert t["unidentified"] == 1                    # XYZZY9 : préfixe inconnu
    assert all(not e["from_qrz"] for e in t["entities"])


def test_dxcc_table_merges_an_entity_known_only_by_qrz() -> None:
    """Préfixe absent de la table + entité donnée par QRZ = MÊME pays.

    Sans cela, PH0DV formait un second « Netherlands », sans drapeau, à côté
    des PA… : le pays comptait double, et le total dépendait de ce que le
    callbook avait déjà récupéré (résultats différents d'une instance à l'autre).
    """
    for call in ("PA1MV", "XYZZY9"):
        _qso(call)
    activation.qrz_lookup("XYZZY9", _FakeQrz({"XYZZY9": _rec("XYZZY9", land="Netherlands",
                                                             dxcc="263")}))
    t = activation.dxcc_table()
    assert t["count"] == 1 and t["unidentified"] == 0
    entity = t["entities"][0]
    assert (entity["code"], entity["dxcc_name"]) == ("NL", "Netherlands")
    assert (entity["stations"], entity["qsos"]) == (2, 2)
    # Le classement des chasseurs montre alors le même drapeau pour les deux.
    assert {h["dxcc_code"] for h in activation.hunters_ranking()} == {"NL"}


def test_dutch_and_british_secondary_prefixes_are_known() -> None:
    """Préfixes qui manquaient à la table : PH0DV n'avait pas de drapeau."""
    from app import dxcc_flags

    for call, code in (("PH0DV", "NL"), ("PC5Q", "NL"), ("2W0ABC", "GB-WLS"),
                       ("2M0ABC", "GB-SCT"), ("OS0AA", "BE"), ("OV3T", "DK")):
        assert dxcc_flags.entity_for_call(call)[0] == code, call


def test_entity_key_falls_back_on_the_callbook_name() -> None:
    from app import dxcc_flags

    assert dxcc_flags.code_for_name("Fed. Rep. of Germany") == "DE"
    assert dxcc_flags.code_for_name("Neverland") == ""
    assert dxcc_flags.entity_key("XYZZY9", "Netherlands")[:2] == ("NL", "NL")
    # Pays inconnu de la table : regroupé sur son nom, mais sans drapeau.
    assert dxcc_flags.entity_key("XYZZY9", "Neverland") == ("neverland", "", "")
    assert dxcc_flags.entity_key("XYZZY9", "") == ("", "", "")


def test_public_page_shows_the_country_flags() -> None:
    _public()
    _qso("DL1ABC")
    activation.set_flag("show_contacts", True)
    page = TestClient(app).get("/tm25test").text
    assert "/static/vendor/flags/de.png" in page and "Germany" in page
    assert "pas encore identifiée" not in page      # le préfixe suffit, sans QRZ


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


def test_map_data_splits_points_by_band_and_mode() -> None:
    """Un point par locator × bande × mode ; deux stations du même carré sur la
    même bande et le même mode ne font qu'un point."""
    _qso("DL1ABC", band="20M", mode="SSB", grid="JO31AB")
    _qso("DL1ABC", band="40M", mode="CW", grid="JO31AB")
    _qso("DL2ABC", band="20M", mode="SSB", grid="JO31CD")   # autre carré : son propre point
    _qso("DL3ABC", band="20M", mode="SSB", grid="JO31AB")
    data = activation.map_data()
    keyed = {(p["grid"], p["band"], p["mode"]): p for p in data["points"]}
    assert ("JO31AB", "20M", "SSB") in keyed and ("JO31AB", "40M", "CW") in keyed
    assert keyed[("JO31AB", "20M", "SSB")]["calls"] == ["DL1ABC", "DL3ABC"]
    assert keyed[("JO31AB", "40M", "CW")]["qsos"] == 1
    assert data["bands"] == ["20M", "40M"] and data["modes"] == ["CW", "SSB"]
    assert data["located"] == 3 and data["stations"] == 3


def test_map_style_defaults_and_reset() -> None:
    style = activation.get_map_style()
    assert style["enabled"] and style["mode_colors"]["SSB"].startswith("#")
    assert style["band_shapes"]["20M"] in activation.MAP_SHAPES
    saved = activation.set_map_style({"enabled": "1", "color_SSB": "#123456",
                                      "shape_20M": "star", "color_CW": "rouge vif",
                                      "shape_40M": "banane"})
    assert saved["mode_colors"]["SSB"] == "#123456" and saved["band_shapes"]["20M"] == "star"
    # Valeurs refusées : on garde les défauts plutôt qu'un style cassé.
    assert saved["mode_colors"]["CW"] == activation.DEFAULT_MAP_STYLE["mode_colors"]["CW"]
    assert saved["band_shapes"]["40M"] == activation.DEFAULT_MAP_STYLE["band_shapes"]["40M"]
    off = activation.set_map_style({"color_SSB": "#123456"})
    assert off["enabled"] is False
    back = activation.set_map_style({"reset": "1"})
    assert back == activation.get_map_style()
    assert back["mode_colors"]["SSB"] == activation.DEFAULT_MAP_STYLE["mode_colors"]["SSB"]


def test_map_style_settings_route_admin_only(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    admin = _private_client()
    r = admin.post("/activation/settings/map", data={"enabled": "1", "color_FT8": "#00ff00",
                                                     "shape_20M": "cross"})
    assert r.status_code == 303 and "mp=ok" in r.headers["location"]
    style = activation.get_map_style()
    assert style["mode_colors"]["FT8"] == "#00ff00" and style["band_shapes"]["20M"] == "cross"
    page = admin.get("/activation/settings").text
    assert "Carte des contacts" in page and 'name="shape_40M"' in page
    activation.set_operator_password("oppass")
    op = TestClient(app, follow_redirects=False)
    op.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    assert op.post("/activation/settings/map", data={"enabled": "1"}).headers["location"].startswith("/login")


def test_public_map_carries_the_style(monkeypatch) -> None:
    _public("JN18FS")
    _qso("DL1ABC", band="20M", mode="CW", grid="JO31AB")
    activation.set_map_style({"enabled": "1", "filters": "1", "color_CW": "#abcdef",
                              "shape_20M": "diamond"})
    page = TestClient(app).get("/tm25test").text
    assert "act-map-legend" in page and "#abcdef" in page and "diamond" in page
    assert '"filters": true' in page and 'data-all=' in page   # cases à cocher bande/mode
    activation.set_map_style({"enabled": "1"})                 # filtres décochés
    assert '"filters": false' in TestClient(app).get("/tm25test").text


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


# ── Comptes opérateurs (mot de passe par opérateur) ────────────────────────


# Mot de passe conforme à la règle (majuscule, chiffre, caractère spécial).
PW = "Motdepasse1!"


def _op_client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _captcha_fields(answer_shift: int = 0, age: int = 5) -> dict[str, str]:
    """Question anti-robot déjà résolue (jeton antidaté : un humain a pris son temps)."""
    ts, good = str(int(time.time()) - age), 7
    return {"captcha_token": f"{ts}.{activation._captcha_sig(ts, good)}",
            "captcha": str(good + answer_shift)}


def _login(client: TestClient, call: str, password: str, **extra: str):
    data = {"callsign": call, "password": password, **_captcha_fields(), **extra}
    return client.post("/activation/login", data=data)


def test_password_hashing_never_stores_clear_text() -> None:
    h = activation.hash_password("s3cret")
    assert "s3cret" not in h and h.startswith("pbkdf2_sha256$")
    assert activation.verify_password("s3cret", h) and not activation.verify_password("s3cre", h)
    assert not activation.verify_password("s3cret", "") and not activation.verify_password("s3cret", "bidon")


def test_per_operator_account_is_created_at_first_login() -> None:
    activation.set_flag("per_operator_auth", True)
    client = _op_client()
    r = _login(client, "f5abc", PW)
    assert r.status_code == 303
    acc = activation.get_operator("F5ABC")
    assert acc["status"] == "active" and acc["active"] == 1 and acc["is_admin"] == 0
    assert acc["password_hash"] and "motdepasse" not in acc["password_hash"]
    assert client.get("/activation").status_code == 200          # session ouverte
    # Mot de passe faux, puis bon : le compte reste celui créé.
    other = _op_client()
    bad = _login(other, "F5ABC", "AutreMdp2!")
    assert bad.status_code == 401 and "Mot de passe incorrect" in bad.text
    assert other.get("/activation").status_code == 303
    assert _login(other, "F5ABC", PW).status_code == 303
    assert other.get("/activation").status_code == 200


def test_new_account_waits_for_approval_when_enabled() -> None:
    activation.set_flag("per_operator_auth", True)
    activation.set_flag("operator_approval", True)
    client = _op_client()
    r = _login(client, "F5NEW", PW)
    assert r.status_code == 401 and "en attente de validation" in r.text
    assert activation.get_operator("F5NEW")["status"] == "pending"
    assert client.get("/activation").status_code == 303          # pas de session
    # Le même mot de passe reste refusé tant que l'admin n'a pas validé.
    assert _login(client, "F5NEW", PW).status_code == 401
    activation.approve_operator("F5NEW")
    assert _login(client, "F5NEW", PW).status_code == 303
    assert client.get("/activation").status_code == 200


def test_roster_operator_sets_password_without_approval() -> None:
    """Ajouté par un admin : déjà approuvé, il choisit juste son mot de passe."""
    activation.set_flag("per_operator_auth", True)
    activation.set_flag("operator_approval", True)
    activation.add_operator("F5ROS", "Jean")
    assert _login(_op_client(), "F5ROS", PW).status_code == 303
    acc = activation.get_operator("F5ROS")
    assert acc["status"] == "active" and acc["name"] == "Jean"


def test_operator_admin_reaches_settings_but_not_site_stats(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("per_operator_auth", True)
    op = _op_client()
    _login(op, "F5OP", PW)
    assert op.get("/activation/settings").status_code == 303     # simple opérateur
    activation.set_operator_admin("F5OP", True)
    page = op.get("/activation/settings")
    assert page.status_code == 200 and "Comptes opérateurs" in page.text
    # Encart réservé à l'admin principal (journal des visites du site, ou des
    # connexions dans l'application autonome) : invisible pour un admin du club.
    site_page = _private_client().get("/activation/settings").text
    reserved = [t for t in ("Fréquentation du site", "Connexions") if t in site_page]
    assert reserved, "encart réservé à l'admin introuvable"
    for title in reserved:
        assert title not in page.text
    # Il peut gérer les comptes…
    assert op.post("/activation/settings/operators/F5ABC", data={"action": "admin"}).status_code == 303
    # …mais pas se retirer ses propres droits.
    r = op.post("/activation/settings/operators/F5OP", data={"action": "unadmin"})
    assert r.headers["location"].endswith("ac=self#comptes")
    assert activation.operator_is_admin("F5OP")


def test_disabled_account_loses_its_session() -> None:
    activation.set_flag("per_operator_auth", True)
    client = _op_client()
    _login(client, "F5OFF", PW)
    assert client.get("/activation").status_code == 200
    activation.set_operator_active("F5OFF", False)
    assert client.get("/activation").status_code == 303          # session invalidée
    assert _login(client, "F5OFF", PW).status_code == 401      # et connexion refusée
    activation.set_operator_active("F5OFF", True)
    assert _login(client, "F5OFF", PW).status_code == 303


def test_session_token_is_bound_to_its_callsign() -> None:
    activation.set_flag("per_operator_auth", True)
    _login(_op_client(), "F5BOB", PW)
    # Jeton du mot de passe commun (sans indicatif) : refusé en mode comptes.
    shared = TestClient(app, follow_redirects=False)
    shared.cookies.set(activation.OP_COOKIE, activation.make_op_token())
    assert shared.get("/activation").status_code == 303
    # Jeton d'un compte inexistant, ou signature d'un autre indicatif : refusés.
    forged = TestClient(app, follow_redirects=False)
    forged.cookies.set(activation.OP_COOKIE, activation.make_op_token("F5GHOST"))
    assert forged.get("/activation").status_code == 303
    token = activation.make_op_token("F5BOB")
    assert activation.session_operator(token) == "F5BOB"
    assert activation.op_session(token.replace("F5BOB", "F5EVE")) is None


def test_admin_resets_and_clears_operator_password() -> None:
    activation.set_flag("per_operator_auth", True)
    _login(_op_client(), "F5RST", "AncienMdp1!")
    activation.set_operator_password_for("F5RST", "NouveauMdp2!")
    assert _login(_op_client(), "F5RST", "AncienMdp1!").status_code == 401
    assert _login(_op_client(), "F5RST", "NouveauMdp2!").status_code == 303
    activation.clear_operator_password("F5RST")                  # oubli : nouveau choix libre
    assert _login(_op_client(), "F5RST", "ToutNeuf3!").status_code == 303
    with pytest.raises(ValueError):
        activation.set_operator_password_for("F5RST", "")
    with pytest.raises(ValueError):
        activation.set_operator_admin("F5NOPE", True)


def test_shared_password_mode_is_unchanged() -> None:
    activation.set_operator_password("commun")
    client = _op_client()
    assert _login(client, "F5CLA", "commun").status_code == 303
    assert client.get("/activation").status_code == 200
    assert "F5CLA" in {o["callsign"] for o in activation.list_operators()}
    assert not activation.get_operator("F5CLA")["password_hash"]


def test_slot_qso_counts_match_operator_band_mode_and_window() -> None:
    activation.set_flag("auto_slots", False)     # on teste le comptage seul
    sid = activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    other = activation.add_slot("F5RRO", "2026-09-07T10:00", "2026-09-07T12:00", "40M", "CW")
    activation.add_operator("F5RRO")
    def qso(call, op="F4IOZ", band="20M", mode="SSB", time_on="1030"):
        activation.add_contact(call=call, band=band, mode=mode, operator_call=op,
                               qso_date="20260907", time_on=time_on)
    qso("DL1ABC")
    qso("DL2ABC", time_on="1159")
    qso("DL3ABC", time_on="1200")          # minute de fin : compté (voir ci-dessous)
    qso("DL4ABC", time_on="1201")          # après la fin
    qso("DL5ABC", time_on="0959")          # avant le début
    qso("DL6ABC", band="40M")              # autre bande
    qso("DL7ABC", mode="CW")               # autre mode
    qso("DL8ABC", op="F5RRO")              # autre opérateur
    qso("DL9ABC", op="F5RRO", band="40M", mode="CW")
    counts = activation.slot_qso_counts()
    assert counts[sid] == 3 and counts[other] == 1
    admin = _private_client()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        planning = admin.get("/activation/planning").text
    assert ">3</td>" in planning or ">3<" in planning
    _public()
    assert "3 QSO" in TestClient(app).get("/tm25test").text


def test_public_shows_every_slot_card_by_default(monkeypatch) -> None:
    """Par défaut, aucune vignette n'est coupée : dix créneaux à venir = dix
    vignettes (avant, la page s'arrêtait à huit)."""
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("auto_slots", False)
    _public()
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M"
    for i in range(10):
        begin = now + timedelta(hours=2 + i)
        activation.add_slot("F4IOZ", begin.strftime(fmt), (begin + timedelta(hours=1)).strftime(fmt),
                            "20M", "SSB", note=f"passe {i}")
    assert activation.public_slots_max() == 0
    page = TestClient(app).get("/tm25test").text
    assert page.count("act-next-card") == 10
    # Limité à 4 depuis les Réglages.
    admin = _private_client()
    admin.post("/activation/settings/flags", data={"show_map_stats": "1", "public_slots_max": "4"})
    assert activation.public_slots_max() == 4
    assert TestClient(app).get("/tm25test").text.count("act-next-card") == 4
    # Valeur absurde : bornée, jamais d'erreur.
    admin.post("/activation/settings/flags", data={"public_slots_max": "n'importe quoi"})
    assert activation.public_slots_max() == 0
    admin.post("/activation/settings/flags", data={"public_slots_max": "99999"})
    assert activation.public_slots_max() == activation.PUBLIC_SLOTS_MAX


def test_settings_button_realigns_slots_on_the_log(monkeypatch) -> None:
    """Bouton « Mettre à jour les créneaux d'après le log » : marche même quand
    le rattrapage automatique est décoché (l'admin le demande explicitement)."""
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("auto_slots", False)
    for call, time_on in (("DL1ABC", "1000"), ("DL2ABC", "1010"), ("DL3ABC", "1020")):
        activation.add_contact(call=call, band="20M", mode="SSB", operator_call="F4IOZ",
                               qso_date="20260907", time_on=time_on)
    assert activation.list_slots() == []              # rien pendant que l'option est off
    admin = _private_client()
    r = admin.post("/activation/settings/slots-from-log")
    assert r.status_code == 303 and "sl=1-0" in r.headers["location"]
    slots = activation.list_slots()
    assert len(slots) == 1 and slots[0]["source"] == "log"
    assert activation.slot_qso_counts()[slots[0]["id"]] == 3
    page = admin.get("/activation/settings?sl=1-0").text
    assert "1 créé(s)" in page and "Mettre à jour les créneaux" in page
    # Réservé à l'admin.
    activation.set_operator_password("oppass")
    op = TestClient(app, follow_redirects=False)
    op.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    assert op.post("/activation/settings/slots-from-log").headers["location"].startswith("/login")


def test_settings_slot_flash_ignores_a_tampered_parameter(monkeypatch) -> None:
    """Le compte rendu vient de l'URL : une valeur bricolée ne doit rien afficher."""
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    page = _private_client().get("/activation/settings?sl=<img src=x onerror=alert(1)>-oops").text
    assert "Créneaux recalés" not in page          # compte rendu non affiché
    assert "<img src=x" not in page                # et jamais réinjecté tel quel


def test_qso_logged_on_the_last_minute_of_a_slot_counts() -> None:
    """Cas vécu (TM25TEST, passage FO-29) : créneau 18:59→19:15, cinq QSO dont
    un à 19:15 pile — la vignette n'en affichait que quatre.

    Les QSO sont horodatés à la minute : celui de 19:15 a eu lieu PENDANT la
    minute de fin, et c'est même le dernier contact du passage."""
    activation.set_flag("auto_slots", False)
    sid = activation.add_slot("F4IOZ", "2026-09-14T18:59", "2026-09-14T19:15", "2M", "SSB",
                              note="SAT FO-29")
    for call, time_on in (("PA3ANG", "1903"), ("F5RRS", "1905"), ("PE1NIL", "1906"),
                          ("F4JFZ", "1910"), ("9A2U", "1915")):
        activation.add_contact(call=call, band="2M", mode="SSB", operator_call="F4IOZ",
                               qso_date="20260914", time_on=time_on, sat_name="FO-29")
    assert activation.slot_qso_counts()[sid] == 5


def test_qso_at_the_hinge_of_two_slots_counts_once() -> None:
    """Deux créneaux qui se touchent : le QSO de la charnière va au plus récent,
    jamais aux deux (sinon le total dépasserait le nombre de QSO)."""
    activation.set_flag("auto_slots", False)
    first = activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T11:00", "20M", "SSB")
    second = activation.add_slot("F4IOZ", "2026-09-07T11:00", "2026-09-07T12:00", "20M", "SSB")
    for call, time_on in (("DL1ABC", "1030"), ("DL2ABC", "1100"), ("DL3ABC", "1130")):
        activation.add_contact(call=call, band="20M", mode="SSB", operator_call="F4IOZ",
                               qso_date="20260907", time_on=time_on)
    counts = activation.slot_qso_counts()
    assert (counts[first], counts[second]) == (1, 2)
    assert counts[first] + counts[second] == 3


def test_slot_without_qso_shows_nothing_rather_than_zero() -> None:
    """Aucun QSO compté = log peut-être pas encore importé : on n'affiche rien."""
    activation.add_slot("F4IOZ", "2026-09-05T10:00", "2026-09-05T12:00", "20M", "SSB")  # passé, vide
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M"
    activation.add_slot("F4IOZ", (now - timedelta(minutes=30)).strftime(fmt),
                        (now + timedelta(hours=1)).strftime(fmt), "40M", "CW")          # en cours, vide
    _public()
    public = TestClient(app).get("/tm25test").text
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_mod, "auth_password", lambda: "secret")
        admin = _private_client()
        board = admin.get("/activation").text
        planning = admin.get("/activation/planning").text
    for page in (public, board, planning):
        # Le compteur d'un créneau n'apparaît que s'il y a des QSO ; le « 0 » du
        # total de l'activation (« QSO réalisés »), lui, reste légitime.
        assert '<b class="act-slot-qso">' not in page and "0 QSO" not in page
    # Dans le planning, la colonne QSO reste simplement vide.
    assert '<td class="act-slot-qso"></td>' in planning


def test_admin_logout_button_is_everywhere_for_the_main_admin(monkeypatch) -> None:
    """Bouton « Admin ⎋ » : visible pour l'admin principal, sur toutes les pages
    de l'espace activation (publiques comprises), et il ferme bien la session."""
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    _public()
    admin = _private_client()
    pages = ("/activation", "/activation/planning", "/activation/settings", "/tm25test",
             "/activations")   # /activation/login redirige quand on est déjà connecté
    for path in pages:
        page = admin.get(path)
        assert page.status_code == 200, path
        assert 'action="/logout"' in page.text and "Admin ⎋" in page.text, path
        assert f'name="next" value="{path}"' in page.text, path
    anon = TestClient(app, follow_redirects=False)
    assert "Admin ⎋" not in anon.get("/tm25test").text          # visiteur : rien
    r = admin.post("/logout", data={"next": "/tm25test"})
    assert r.status_code == 303 and r.headers["location"] == "/tm25test"
    # Le cookie admin est effacé (le client de test garde celui posé à la main).
    cookie = r.headers.get("set-cookie", "")
    assert auth_mod.COOKIE_NAME in cookie and "Max-Age=0" in cookie
    assert TestClient(app, follow_redirects=False).get("/activation/settings").status_code == 303


def test_password_rule_is_enforced_at_account_creation() -> None:
    activation.set_flag("per_operator_auth", True)
    for weak in ("motdepasse", "Motdepasse", "Motdepas1", "Mdp1!", "motdepasse1!"):
        r = _login(_op_client(), "F5WEAK", weak)
        assert r.status_code == 401, weak
        assert "majuscule" in r.text and activation.get_operator("F5WEAK") is None, weak
    assert _login(_op_client(), "F5WEAK", PW).status_code == 303
    assert activation.get_operator("F5WEAK")["password_hash"]
    # La règle en vigueur est affichée sur la page de connexion.
    assert "Au moins 8 caractères, dont 1 majuscule, 1 chiffre, 1 caractère spécial." \
        in _op_client().get("/activation/login").text
    # Un administrateur ne peut pas poser un mot de passe trop simple non plus.
    with pytest.raises(ValueError, match="majuscule"):
        activation.set_operator_password_for("F5WEAK", "faible")


def test_activity_report_pdf(monkeypatch) -> None:
    """Rapport PDF : réservé à l'admin, lisible, et présent dans les Réglages."""
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("auto_slots", False)
    for i, call in enumerate(("DL1ABC", "ON4ZZ", "G0XYZ", "EA5QQ")):
        activation.add_contact(call=call, band="20M", mode="SSB", operator_call="F4IOZ",
                               qso_date="20260910", time_on=f"10{i:02d}", gridsquare="JO31")
    admin = _private_client()
    r = admin.get("/activation/report.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "tm25test-rapport.pdf" in r.headers["content-disposition"]
    body = r.content
    assert body.startswith(b"%PDF-1.") and body.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in body and b"/Type /Page " in body
    assert b"(TM25TEST)" in body                      # le titre de l'activation
    assert b"(529)" not in body and b"(4)" in body     # le nombre de QSO du log de test
    # Indicatif inconnu : on revient aux Réglages, pas d'erreur 500.
    assert admin.get("/activation/report.pdf?station=XX9ZZZ").status_code == 303
    assert '/activation/report.pdf"' in admin.get("/activation/settings").text
    # Espace opérateurs : refusé.
    activation.set_operator_password("oppass")
    op = TestClient(app, follow_redirects=False)
    op.post("/activation/login", data={"callsign": "F5TEST", "password": "oppass"})
    assert op.get("/activation/report.pdf").headers["location"].startswith("/login")


def test_report_options_shape_the_pdf(monkeypatch, tmp_path) -> None:
    """Sections facultatives : rythme horaire, entités avec drapeaux, chasseurs."""
    monkeypatch.setattr(activation, "LOGO_DIR", tmp_path / "branding")
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("auto_slots", False)
    for i, call in enumerate(("DL1ABC", "ON4ZZ", "G0XYZ", "EA5QQ", "I2WWW")):
        activation.add_contact(call=call, band="20M", mode="SSB", operator_call="F4IOZ",
                               qso_date="20260910", time_on=f"10{i:02d}")
    admin = _private_client()
    assert activation.get_report_options() == activation.DEFAULT_REPORT
    base = admin.get("/activation/report.pdf").content
    assert b"RYTHME, HEURE PAR HEURE" not in base      # coupé par défaut (titres en capitales)
    assert b"/Subtype /Image" in base                  # les drapeaux des entités

    r = admin.post("/activation/settings/report",
                   data={"hours": "1", "dxcc_all": "", "hunters": "0"})
    assert r.status_code == 303 and "rp=ok" in r.headers["location"]
    assert activation.get_report_options() == {"hours": True, "dxcc_all": False, "hunters": 0,
                                               "sats": False, "one_page": False}
    tuned = admin.get("/activation/report.pdf").content
    assert b"RYTHME, HEURE PAR HEURE" in tuned
    assert b"MEILLEURS CHASSEURS" not in tuned         # palmarès retiré
    assert b"/Subtype /Image" not in tuned             # tableau court, sans drapeau

    admin.post("/activation/settings/report", data={"dxcc_all": "1", "hunters": "3"})
    short = admin.get("/activation/report.pdf").content
    assert b"MEILLEURS CHASSEURS" in short
    # Valeur absurde : bornée, jamais d'erreur.
    admin.post("/activation/settings/report", data={"hunters": "5000"})
    assert activation.get_report_options()["hunters"] == activation.REPORT_HUNTERS_MAX


def _pages(pdf_bytes: bytes) -> int:
    return pdf_bytes.count(b"/Type /Page ")


def test_report_fits_one_page_on_demand(monkeypatch, tmp_path) -> None:
    """Option « une seule page » : le rapport se resserre, puis coupe les listes."""
    monkeypatch.setattr(activation, "LOGO_DIR", tmp_path / "branding")
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("auto_slots", False)
    # Un log copieux : beaucoup d'entités et de chasseurs, deux pages au large.
    prefixes = ("F", "ON", "PA", "DL", "G", "EA", "I", "CT", "OE", "HB9", "SP", "OK", "OM",
                "HA", "S5", "9A", "YU", "SV", "LZ", "YO", "OH", "SM", "LA", "OZ", "ES",
                "LY", "YL", "UR", "RA", "K", "VE", "PY", "JA", "VK", "ZS")
    calls = [f"{prefix}{n}AB" for prefix in prefixes for n in range(1, 4)]
    for i, call in enumerate(calls):
        activation.add_contact(call=call, band="20M", mode="SSB", operator_call="F4IOZ",
                               qso_date="2026091%d" % (i % 10), time_on=f"{8 + i % 12:02d}30")
    admin = _private_client()
    admin.post("/activation/settings/report", data={"dxcc_all": "1", "hunters": "100", "sats": "1"})
    large = admin.get("/activation/report.pdf").content
    assert _pages(large) >= 2

    admin.post("/activation/settings/report",
               data={"dxcc_all": "1", "hunters": "100", "sats": "1", "one_page": "1"})
    assert activation.get_report_options()["one_page"] is True
    one = admin.get("/activation/report.pdf").content
    assert _pages(one) == 1
    assert b"ENTIT" in one and b"MEILLEURS CHASSEURS" in one   # les sections restent
    assert len(one) < len(large)                    # coupé, pas déplacé sur une 2e page


def test_logo_keeps_its_transparency_in_the_pdf(monkeypatch, tmp_path) -> None:
    """Un logo détouré doit le rester sur le bandeau : le PDF porte un masque
    (avant, l'alpha était aplati sur du blanc — rectangle blanc sur fond bleu)."""
    from app import pdf as pdf_mod

    monkeypatch.setattr(activation, "LOGO_DIR", tmp_path / "branding")
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    # Petit PNG rond détouré : 8 pixels, deux opaques, deux transparents.
    import zlib
    width = height = 2
    raw = b"".join(b"\x00" + bytes((255, 0, 0, 255, 0, 255, 0, 0)) for _ in range(height))
    def chunk(name: bytes, body: bytes) -> bytes:
        return (len(body).to_bytes(4, "big") + name + body
                + zlib.crc32(name + body).to_bytes(4, "big"))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big")
                   + bytes((8, 6, 0, 0, 0)))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    w, h, rgb, alpha = pdf_mod.read_png(png)
    assert (w, h) == (2, 2) and alpha == bytes((255, 0, 255, 0))
    assert rgb[:3] == bytes((255, 0, 0))          # la couleur n'est pas délavée

    activation.set_logo(png)
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    body = _private_client().get("/activation/report.pdf").content
    assert b"/SMask" in body                      # transparence conservée


def test_report_footer_is_signed(monkeypatch, tmp_path) -> None:
    """Pied de page : la signature du logiciel, sur chaque page."""
    monkeypatch.setattr(activation, "LOGO_DIR", tmp_path / "branding")
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    body = _private_client().get("/activation/report.pdf").content
    assert body.count(b"TM-Activation") >= 1
    assert b"F4IOZ" in body


def test_report_details_satellite_qsos(monkeypatch, tmp_path) -> None:
    """Section « Satellites » : présente dès qu'il y a des QSO satellite,
    désactivable, et absente quand la station n'a rien fait par satellite."""
    monkeypatch.setattr(activation, "LOGO_DIR", tmp_path / "branding")
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("auto_slots", False)
    admin = _private_client()
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    assert b"SATELLITES" not in admin.get("/activation/report.pdf").content   # rien en sat
    for call, sat in (("ON4ZZ", "FO-29"), ("G0XYZ", "FO-29"), ("EA5QQ", "SO-50")):
        activation.add_contact(call=call, band="2M", mode="SSB", operator_call="F4IOZ",
                               sat_name=sat)
    sats = activation.satellite_stats()
    assert [s["sat"] for s in sats["by_sat"]] == ["FO-29", "SO-50"]
    assert sats["total"] == 3 and sats["count"] == 2 and sats["share"] == 75.0
    body = admin.get("/activation/report.pdf").content
    assert b"SATELLITES" in body and b"(FO-29)" in body and b"(SO-50)" in body
    # Décochée dans les Réglages : la section disparaît.
    admin.post("/activation/settings/report", data={"dxcc_all": "1", "hunters": "10"})
    assert activation.get_report_options()["sats"] is False
    assert b"SATELLITES" not in admin.get("/activation/report.pdf").content


def test_club_logo_upload_and_use(monkeypatch, tmp_path) -> None:
    """Logo : déposé dans les Réglages, servi sur /logo, repris dans le PDF."""
    monkeypatch.setattr(activation, "LOGO_DIR", tmp_path / "branding")
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    client = TestClient(app)
    assert client.get("/logo").status_code == 404
    png = (Path(__file__).resolve().parent.parent / "static" / "vendor" / "flags" / "fr.png")
    admin = _private_client()
    r = admin.post("/activation/settings/logo",
                   files={"logo": ("logo.png", png.read_bytes(), "image/png")})
    assert r.status_code == 303 and "lg=ok" in r.headers["location"]
    served = client.get("/logo")
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert activation.logo_info()["kind"] == "png"
    assert "/logo?v=" in client.get("/").text            # en-tête du site
    assert b"/Subtype /Image" in admin.get("/activation/report.pdf").content
    # Fichier qui n'est pas une image : refusé avec un message.
    bad = admin.post("/activation/settings/logo",
                     files={"logo": ("notes.txt", b"bonjour", "text/plain")})
    assert "lg=format" in bad.headers["location"] or "PNG" in bad.headers["location"]
    assert activation.logo_info() is not None            # l'ancien logo est gardé
    assert admin.post("/activation/settings/logo/delete").status_code == 303
    assert activation.logo_info() is None and client.get("/logo").status_code == 404


def test_activity_report_survives_an_empty_log(monkeypatch) -> None:
    """Rapport demandé avant le premier QSO : une page valide, pas une erreur."""
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    body = _private_client().get("/activation/report.pdf").content
    assert body.startswith(b"%PDF-1.") and b"(0)" in body


def test_planning_highlights_the_booking_form(monkeypatch) -> None:
    """La carte « Réserver un créneau » est encadrée : c'est le geste principal
    de la page, il ne doit pas se confondre avec « Ajouter un opérateur »."""
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    page = _private_client().get("/activation/planning").text
    assert 'class="act-card act-card-focus"' in page
    assert page.index("act-card-focus") < page.index("Réserver un créneau")


def test_password_requirements_are_adjustable(monkeypatch) -> None:
    """Réglages → Comptes opérateurs : longueur, majuscules, chiffres et
    caractères spéciaux exigés (0 = pas d'exigence)."""
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("per_operator_auth", True)
    assert activation.get_password_rule() == activation.DEFAULT_PASSWORD_RULE
    admin = _private_client()
    r = admin.post("/activation/settings/auth",
                   data={"per_operator": "1", "min_length": "12", "min_upper": "2",
                         "min_digits": "0", "min_special": "3"})
    assert r.status_code == 303
    assert activation.get_password_rule() == {"min_length": 12, "min_upper": 2,
                                              "min_digits": 0, "min_special": 3}
    assert activation.password_rule() == "Au moins 12 caractères, dont 2 majuscules, 3 caractères spéciaux."
    assert not activation.password_is_strong("Abcdef1!")          # trop court, une seule majuscule
    assert not activation.password_is_strong("ABcdefgh!$")        # deux spéciaux sur trois
    assert activation.password_is_strong("ABcdefghi!$%")          # 12 car., 2 maj., 3 spéciaux
    # Le refus de connexion reprend la règle réglée.
    assert "2 majuscules" in _login(_op_client(), "F5NEW", "Abcdef1!").text
    assert _login(_op_client(), "F5NEW", "ABcdefghi!$%").status_code == 303
    # Aucune exigence de caractères : seule la longueur compte.
    admin.post("/activation/settings/auth",
               data={"per_operator": "1", "min_length": "6", "min_upper": "0",
                     "min_digits": "0", "min_special": "0"})
    assert activation.password_rule() == "Au moins 6 caractères."
    assert activation.password_is_strong("abcdef") and not activation.password_is_strong("abcde")
    # Valeurs bricolées : on retombe sur le défaut plutôt que d'ouvrir la porte.
    admin.post("/activation/settings/auth",
               data={"per_operator": "1", "min_length": "1", "min_upper": "99",
                     "min_digits": "n'importe quoi", "min_special": "-3"})
    rule = activation.get_password_rule()
    assert rule["min_length"] == 4 and rule["min_upper"] == activation.PASSWORD_COUNT_MAX
    assert rule["min_digits"] == 1 and rule["min_special"] == 0


def test_robot_signups_are_blocked() -> None:
    activation.set_flag("per_operator_auth", True)
    page = _op_client().get("/activation/login").text
    assert "Question anti-robot" in page and 'name="captcha_token"' in page and 'name="website"' in page
    base = {"callsign": "F5BOT", "password": PW}

    def post(**extra):
        return _op_client().post("/activation/login", data={**base, **extra})

    # Sans question résolue, mauvaise réponse, jeton bricolé : rien n'est créé.
    for extra in ({}, _captcha_fields(answer_shift=1), {"captcha": "7", "captcha_token": "1.2"},
                  {"captcha": "7", "captcha_token": f"{int(time.time())}.deadbeef"}):
        r = post(**extra)
        assert r.status_code == 401 and "question" in r.text.lower()
        assert activation.get_operator("F5BOT") is None
    # Formulaire renvoyé instantanément (robot) : refusé malgré la bonne réponse.
    assert post(**_captcha_fields(age=0)).status_code == 401
    # Jeton périmé : refusé.
    assert post(**_captcha_fields(age=activation.CAPTCHA_TTL + 60)).status_code == 401
    # Champ-piège rempli : refusé, même avec la bonne réponse.
    assert post(**_captcha_fields(), website="https://spam.example").status_code == 401
    assert activation.get_operator("F5BOT") is None
    # Réponse correcte, formulaire rempli normalement : compte créé.
    assert post(**_captcha_fields()).status_code == 303
    assert activation.get_operator("F5BOT")["password_hash"]


def test_captcha_only_in_per_operator_mode() -> None:
    """Mot de passe commun : pas de question (le compte n'est pas créé ici)."""
    activation.set_operator_password("commun")
    page = _op_client().get("/activation/login").text
    assert "Question anti-robot" not in page and 'name="captcha_token"' not in page
    client = _op_client()
    r = client.post("/activation/login", data={"callsign": "F5CLA", "password": "commun"})
    assert r.status_code == 303 and client.get("/activation").status_code == 200


# ── « Station déjà contactée ? » pendant la saisie du log ──────────────────


def test_worked_before_reports_history_of_a_station() -> None:
    assert activation.worked_before("DL1ABC") == {
        "call": "DL1ABC", "worked": 0, "band_modes": [], "last": "", "elsewhere": []}
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ",
                           qso_date="20260907", time_on="1030")
    activation.add_contact(call="DL1ABC", band="40M", mode="CW", operator_call="F4IOZ",
                           qso_date="20260908", time_on="0915")
    activation.add_contact(call="DL1ABC", band="40M", mode="CW", operator_call="F5RRO",
                           qso_date="20260908", time_on="0920")
    w = activation.worked_before("dl1abc")
    assert w["worked"] == 3 and w["last"] == "08/09/26 09:20"
    assert sorted(w["band_modes"]) == ["20M SSB", "40M CW"] and w["elsewhere"] == []
    # QSO sous un AUTRE indicatif du club : signalé à part (ce n'est pas un doublon).
    activation.create_station("TM61TEST")
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ",
                           qso_date="20260909", time_on="1000", station="TM61TEST")
    w = activation.worked_before("DL1ABC")
    assert w["worked"] == 3 and w["elsewhere"] == [{"station": "TM61TEST", "n": 1}]
    assert activation.worked_before("!!")["worked"] == 0


def test_worked_route_needs_the_operator_area(monkeypatch) -> None:
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/activation/worked?call=DL1ABC").status_code == 303
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    d = _private_client().get("/activation/worked?call=dl1abc").json()
    assert d["call"] == "DL1ABC" and d["worked"] == 1 and d["band_modes"] == ["20M SSB"]


def test_log_page_carries_the_worked_hint(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    page = _private_client().get("/activation/log").text
    assert 'id="act-worked-hint"' in page and "/activation/worked?call=" in page
    for text in ("Station jamais contactée", "Déjà contactée", "doublon"):
        assert text in page, text


# ── Créneaux déduits du log ────────────────────────────────────────────────


def _qso_at(call: str, when: str, op: str = "F4IOZ", band: str = "20M", mode: str = "SSB") -> None:
    """QSO à une heure UTC donnée (« AAAAMMJJ HHMM »)."""
    date, time_on = when.split()
    activation.add_contact(call=call, band=band, mode=mode, operator_call=op,
                           qso_date=date, time_on=time_on)


def test_forgotten_slot_is_created_from_the_log() -> None:
    assert activation.list_slots() == []
    _qso_at("DL1ABC", "20260907 1003")
    _qso_at("DL2ABC", "20260907 1027")
    slots = activation.list_slots()
    assert len(slots) == 1
    slot = slots[0]
    # Calé sur le quart d'heure, opérateur, bande et mode repris du log.
    assert (slot["start_utc"], slot["end_utc"]) == ("2026-09-07T10:00", "2026-09-07T10:30")
    assert slot["operator_call"] == "F4IOZ" and slot["band"] == "20M" and slot["mode"] == "SSB"
    assert slot["source"] == "log"


def test_slot_is_extended_when_the_operator_runs_over() -> None:
    sid = activation.add_slot("F4IOZ", "2026-09-07T10:00", "2026-09-07T12:00", "20M", "SSB")
    _qso_at("DL1ABC", "20260907 1130")
    _qso_at("DL2ABC", "20260907 1242")            # bien après la fin prévue
    slot = activation.get_slot(sid)
    assert slot["end_utc"] == "2026-09-07T12:45" and slot["start_utc"] == "2026-09-07T10:00"
    assert slot["source"] == "manual" and len(activation.list_slots()) == 1
    # Un QSO avant l'heure prévue étire le début, sans jamais raccourcir.
    _qso_at("DL3ABC", "20260907 0940")
    slot = activation.get_slot(sid)
    assert slot["start_utc"] == "2026-09-07T09:30" and slot["end_utc"] == "2026-09-07T12:45"


def test_separate_sessions_make_separate_slots() -> None:
    _qso_at("DL1ABC", "20260907 1000")
    _qso_at("DL2ABC", "20260907 1020")            # même séance (< 30 min)
    _qso_at("DL3ABC", "20260907 1400")            # séance du soir
    _qso_at("DL4ABC", "20260907 1015", band="40M", mode="CW")   # autre bande/mode
    _qso_at("DL5ABC", "20260907 1010", op="F5RRO")              # autre opérateur
    slots = sorted(activation.list_slots(), key=lambda s: (s["operator_call"], s["band"], s["start_utc"]))
    assert len(slots) == 4
    assert [(s["operator_call"], s["band"], s["mode"], s["start_utc"]) for s in slots] == [
        ("F4IOZ", "20M", "SSB", "2026-09-07T10:00"),
        ("F4IOZ", "20M", "SSB", "2026-09-07T14:00"),
        ("F4IOZ", "40M", "CW", "2026-09-07T10:15"),
        ("F5RRO", "20M", "SSB", "2026-09-07T10:00"),
    ]


def test_auto_slots_can_be_switched_off(monkeypatch) -> None:
    activation.set_flag("auto_slots", False)
    _qso_at("DL1ABC", "20260907 1000")
    assert activation.list_slots() == []
    assert activation.reconcile_slots_from_log() == {"created": 0, "extended": 0}
    activation.set_flag("auto_slots", True)
    assert activation.reconcile_slots_from_log() == {"created": 1, "extended": 0}
    # Le réglage est bien piloté depuis les Réglages (admin).
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    admin = _private_client()
    admin.post("/activation/settings/flags", data={"show_contacts": "1"})
    assert activation.auto_slots() is False
    admin.post("/activation/settings/flags", data={"auto_slots": "1"})
    assert activation.auto_slots() is True


def test_log_page_shows_the_current_slots(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M"
    activation.add_slot("F5RRO", (now - timedelta(minutes=20)).strftime(fmt),
                        (now + timedelta(hours=1)).strftime(fmt), "20M", "SSB")
    activation.add_slot("F4IOZ", (now + timedelta(hours=3)).strftime(fmt),
                        (now + timedelta(hours=5)).strftime(fmt), "40M", "CW")
    admin = _private_client()
    admin.post("/activation/whoami", data={"operator": "F5RRO", "next": "/activation/log"})
    page = admin.get("/activation/log").text
    assert "Créneaux du moment" in page and "F5RRO" in page and "F4IOZ" in page
    assert "is-me" in page                      # l'opérateur connecté est mis en avant


# ── Bande/mode réservés par un autre opérateur ─────────────────────────────


def _now_slot(op: str, band: str, mode: str, start_min: int = -30, end_min: int = 60) -> int:
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M"
    return activation.add_slot(op, (now + timedelta(minutes=start_min)).strftime(fmt),
                               (now + timedelta(minutes=end_min)).strftime(fmt), band, mode)


def test_blocking_slot_only_for_another_operator() -> None:
    _now_slot("F5RRO", "20M", "SSB")
    assert activation.blocking_slot("F4IOZ", "20M", "SSB")["operator_call"] == "F5RRO"
    assert activation.blocking_slot("F5RRO", "20M", "SSB") is None      # son propre créneau
    assert activation.blocking_slot("F4IOZ", "40M", "SSB") is None      # autre bande
    assert activation.blocking_slot("F4IOZ", "20M", "CW") is None       # autre mode
    # Hors de la tranche horaire, plus rien ne bloque.
    later = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    assert activation.blocking_slot("F4IOZ", "20M", "SSB", later) is None


def test_logging_is_refused_on_a_reserved_band_and_mode(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    _now_slot("F5RRO", "20M", "SSB")
    admin = _private_client()
    r = admin.post("/activation/contacts",
                   data={"call": "DL1ABC", "band": "20M", "mode": "SSB", "operator": "F4IOZ", "now": "1"})
    assert r.status_code == 200 and "réservé par F5RRO" in r.text
    assert activation.list_contacts() == []                  # rien n'est écrit
    # Sur une autre bande, le QSO passe.
    r = admin.post("/activation/contacts",
                   data={"call": "DL1ABC", "band": "40M", "mode": "SSB", "operator": "F4IOZ", "now": "1"})
    assert "réservé par" not in r.text and len(activation.list_contacts()) == 1
    # L'opérateur qui a réservé loggue normalement sur son créneau.
    r = admin.post("/activation/contacts",
                   data={"call": "DL2ABC", "band": "20M", "mode": "SSB", "operator": "F5RRO", "now": "1"})
    assert "réservé par" not in r.text and len(activation.list_contacts()) == 2


def test_slot_lock_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    _now_slot("F5RRO", "20M", "SSB")
    activation.set_flag("slot_lock", False)
    admin = _private_client()
    r = admin.post("/activation/contacts",
                   data={"call": "DL1ABC", "band": "20M", "mode": "SSB", "operator": "F4IOZ", "now": "1"})
    assert "réservé par" not in r.text and len(activation.list_contacts()) == 1
    assert admin.get("/activation/slot-conflict?band=20M&mode=SSB").json() == {"blocked": False}
    activation.set_flag("slot_lock", True)
    d = admin.get("/activation/slot-conflict?band=20M&mode=SSB").json()
    assert d["blocked"] and d["operator"] == "F5RRO" and d["band"] == "20M"


def test_slot_conflict_route_needs_the_operator_area() -> None:
    assert TestClient(app, follow_redirects=False).get(
        "/activation/slot-conflict?band=20M&mode=SSB").status_code == 303


# ── Spots DX et drapeaux des pays contactés ────────────────────────────────


def test_entity_for_call_reads_the_prefix() -> None:
    from app import dxcc_flags

    assert dxcc_flags.entity_for_call("F4IOZ") == ("FR", "France")
    assert dxcc_flags.entity_for_call("TM25TEST")[1] == "France"
    assert dxcc_flags.entity_for_call("EA8XX")[1] == "Canary Islands"   # préfixe long d'abord
    assert dxcc_flags.entity_for_call("GM4ABC") == ("GB-SCT", "Scotland")
    assert dxcc_flags.entity_for_call("F4IOZ/P")[0] == "FR"             # suffixe portable ignoré
    assert dxcc_flags.entity_for_call("F/DL1ABC")[1] == "France"        # préfixe portable = pays d'émission
    assert dxcc_flags.entity_for_call("XYZZY") == ("", "")              # inconnu : pas de drapeau
    assert dxcc_flags.flag("FR") == "🇫🇷" and dxcc_flags.flag("GB-SCT") == ""


def test_overseas_and_island_entities_are_not_merged() -> None:
    """La Corse, les Canaries, la Sicile… sont des entités DXCC à part entière,
    au même titre que la Guadeloupe ou la Guyane : jamais fondues dans le pays."""
    from app import dxcc_flags

    for call, code, name in (("TK5MH", "FR-COR", "Corsica"), ("FG8OJ", "GP", "Guadeloupe"),
                             ("FY5KE", "GF", "French Guiana"), ("EA8XX", "ES-CN", "Canary Islands"),
                             ("EA6AA", "ES-IB", "Balearic Islands"), ("IT9ABC", "IT-SIC", "Sicily"),
                             ("IS0ABC", "IT-SAR", "Sardinia"), ("CT3FN", "PT-MAD", "Madeira"),
                             ("CU2AA", "PT-AZO", "Azores"), ("KL7AA", "US-AK", "Alaska"),
                             ("KH6XX", "US-HI", "Hawaii"), ("RA2FF", "RU-KGD", "Kaliningrad")):
        assert dxcc_flags.entity_for_call(call) == (code, name), call
    metropoles = {dxcc_flags.entity_for_call(c)[0] for c in ("F4IOZ", "EA1AA", "I1AAA", "CT1AA",
                                                             "W1AW", "UA3XX")}
    assert metropoles == {"FR", "ES", "IT", "PT", "US", "RU"}   # aucune confusion avec les îles


def test_every_dxcc_entity_has_its_own_name() -> None:
    """Deux entités ne doivent jamais partager un code (elles seraient comptées
    comme un seul pays dans le tableau DXCC)."""
    from collections import defaultdict

    from app import dxcc_flags

    noms = defaultdict(set)
    for code, name in dxcc_flags.PREFIXES.values():
        noms[code].add(name)
    fusionnees = {code: sorted(n) for code, n in noms.items() if len(n) > 1}
    assert not fusionnees, f"entités fondues ensemble : {fusionnees}"


def test_every_dxcc_code_has_a_flag_image() -> None:
    """Les vignettes sont servies par l'application (Windows n'affiche pas les
    emojis drapeaux) : chaque préfixe doit avoir son image dans static/vendor."""
    from pathlib import Path

    from app import dxcc_flags

    flags = Path(__file__).resolve().parent.parent / "static" / "vendor" / "flags"
    manquants = sorted({code for code, _n in dxcc_flags.PREFIXES.values()
                        if not (flags / f"{dxcc_flags.flag_file(code)}.png").exists()})
    assert not manquants, ("vignettes absentes (relancer packaging/tm-activation/fetch_flags.py) : "
                           + ", ".join(manquants))


def test_worked_entities_lists_countries_most_recent_first() -> None:
    assert activation.worked_entities() == []
    for call, date, t in (("DL1ABC", "20260101", "1200"), ("DL2ABC", "20260101", "1300"),
                          ("VE1ZZ", "20260102", "0900"), ("XYZZY9", "20260103", "1000")):
        activation.add_contact(call=call, band="20M", mode="SSB", operator_call="F4IOZ",
                               qso_date=date, time_on=t)
    ents = activation.worked_entities()
    assert [e["code"] for e in ents] == ["CA", "DE"]     # XYZZY9 : entité inconnue, ignorée
    assert ents[0]["name"] == "Canada" and ents[0]["n"] == 1
    assert ents[1]["n"] == 2 and ents[1]["calls"] == 2   # deux indicatifs allemands
    assert activation.worked_entities(limit=1) == [ents[0]]


def test_log_page_shows_the_worked_country_flags(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    admin = _private_client()
    page = admin.get("/activation/log").text
    assert "Pays contactés" not in page                  # aucun QSO : pas d'encart
    r = admin.post("/activation/contacts",
                   data={"call": "DL1ABC", "band": "20M", "mode": "SSB", "operator": "F4IOZ", "now": "1"})
    assert "Pays contactés" in r.text and "Germany" in r.text
    assert "/static/vendor/flags/de.png" in r.text       # vignette locale, pas un emoji


def test_spots_panel_survives_a_network_outage(monkeypatch) -> None:
    """Sans Internet, le panneau est simplement vide : la page de log reste utilisable."""
    from app import dx_spots as spots

    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    spots.clear_cache()

    def boom(*_a, **_k):
        raise OSError("pas de réseau")

    monkeypatch.setattr(spots.httpx, "get", boom)
    assert spots.recent_spots("TM25TEST", force=True) == []
    r = _private_client().get("/activation/spots")
    assert r.status_code == 200 and r.text.strip() == ""


def test_spots_panel_lists_the_spots(monkeypatch) -> None:
    from app import dx_spots as spots

    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    monkeypatch.setattr(
        spots, "recent_spots",
        lambda call, limit=5, force=False: [
            {"dx": call, "freq_khz": 14190.0, "spotter": "K4NYX", "comment": "loud in FL",
             "when": "1433z 20 Sep", "age_min": 3, "band": "20M", "mode": "PHONE"}
        ],
    )
    client = _private_client()
    page = client.get("/activation/spots").text
    assert "14190.0" in page and "K4NYX" in page and "20M" in page and "il y a 3 min" in page
    assert "actUseSpot('14.190', '20M')" in page      # clic → fréquence reprise dans le formulaire
    assert "act-card" not in page                     # panneau intégré à la carte de l'opérateur
    assert "loud in FL" not in page                   # le commentaire n'est plus affiché
    # Le panneau ne montre que la bande et le type de trafic en cours.
    assert "14190.0" in client.get("/activation/spots?band=20M").text
    assert client.get("/activation/spots?band=40M").text.strip() == ""
    assert "14190.0" in client.get("/activation/spots?band=20M&mode=SSB").text
    assert client.get("/activation/spots?band=20M&mode=CW").text.strip() == ""
    assert client.get("/activation/spots?band=20M&mode=FT8").text.strip() == ""


def test_spot_of_unknown_mode_is_kept(monkeypatch) -> None:
    """Mieux vaut montrer un spot au mode indéterminé que laisser croire que
    personne ne nous entend."""
    from app import dx_spots as spots

    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    monkeypatch.setattr(spots, "recent_spots", lambda call, limit=5, force=False: [
        {"dx": call, "freq_khz": 14190.0, "spotter": "K4NYX", "comment": "", "when": "",
         "age_min": 2, "band": "20M", "mode": ""}])
    assert "K4NYX" in _private_client().get("/activation/spots?band=20M&mode=CW").text


def test_spot_mode_from_the_comment_then_the_band_plan() -> None:
    from app import dx_spots

    assert dx_spots.mode_family(7005) == "CW"           # bas de bande
    assert dx_spots.mode_family(7044) == "DIGI"         # segment numérique
    assert dx_spots.mode_family(7188) == "PHONE"
    assert dx_spots.mode_family(7188, "FT8 -06db") == "DIGI"    # le commentaire prime
    assert dx_spots.mode_family(14250, "SES special call") == "PHONE"
    assert dx_spots.mode_family(50313) == "DIGI" and dx_spots.mode_family(144174) == "DIGI"
    assert dx_spots.mode_family(3600) == "PHONE" and dx_spots.mode_family(7040) == "DIGI"
    assert dx_spots.mode_family(12345) == ""            # hors bande amateur
    familles = {m: dx_spots.family_of_mode(m) for m in activation.MODES}
    assert familles == {"SSB": "PHONE", "FM": "PHONE", "AM": "PHONE", "CW": "CW",
                        "FT8": "DIGI", "FT4": "DIGI", "RTTY": "DIGI", "PSK31": "DIGI",
                        "SSTV": "DIGI", "DIGI": "DIGI"}


def test_spots_needs_the_operator_area() -> None:
    assert TestClient(app, follow_redirects=False).get("/activation/spots").status_code == 303


def test_spot_band_and_age(monkeypatch) -> None:
    from app import dx_spots as spots

    assert spots.band_of(14190.0) == "20M" and spots.band_of(144300.0) == "2M"
    assert spots.band_of(12345.0) == ""
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(minutes=7)).strftime("%H%Mz %d %b")
    assert spots._age_minutes(stamp) in (6, 7, 8)
    assert spots._age_minutes("n'importe quoi") is None


def test_spots_are_cached_between_calls(monkeypatch) -> None:
    from app import dx_spots as spots

    spots.clear_cache()
    calls = []

    def fake(call, limit):
        calls.append(call)
        return [{"dx": call, "freq_khz": 14190.0, "spotter": "K4NYX", "comment": "",
                 "when": "", "age_min": None, "band": "20M"}]

    monkeypatch.setattr(spots, "_from_dxwatch", fake)
    spots.recent_spots("TM25TEST")
    spots.recent_spots("TM25TEST")
    assert calls == ["TM25TEST"]                 # deuxième appel servi par le cache
    spots.recent_spots("TM25TEST", force=True)
    assert calls == ["TM25TEST", "TM25TEST"]
    spots.clear_cache()


# ── Jauges de cadence ──────────────────────────────────────────────────────


def _qso_minutes_ago(call: str, minutes: int, operator: str = "F4IOZ") -> None:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    activation.add_contact(call=call, band="20M", mode="SSB", operator_call=operator,
                           qso_date=when.strftime("%Y%m%d"), time_on=when.strftime("%H%M"))


def test_qso_rate_counts_both_windows() -> None:
    activation.set_flag("auto_slots", False)     # pas de créneaux déduits ici
    for i, minutes in enumerate((2, 5, 9, 20, 40)):
        _qso_minutes_ago(f"DL{i}ABC", minutes)
    rate = activation.qso_rate()
    assert rate["hour"]["qsos"] == 5 and rate["hour"]["per_hour"] == 5.0
    assert rate["ten"]["qsos"] == 3 and rate["ten"]["per_hour"] == 18.0
    assert rate["ten"]["gauge"] == 30                    # 18 QSO/h sur une pleine échelle de 60
    assert rate["hour"]["level"] == "station calme"    # 5 QSO/h
    assert rate["ten"]["level"] == "bon rythme"        # 18 QSO/h projetés


def test_qso_rate_counts_the_qso_just_logged() -> None:
    """Le QSO enregistré à la minute même doit faire bouger la jauge aussitôt."""
    activation.set_flag("auto_slots", False)
    assert activation.qso_rate()["ten"]["qsos"] == 0
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ")
    assert activation.qso_rate()["ten"]["qsos"] == 1


def test_qso_rate_trend_compares_with_the_previous_period() -> None:
    activation.set_flag("auto_slots", False)
    for i, minutes in enumerate((3, 6, 75, 80, 90)):     # 2 dans l'heure, 3 avant
        _qso_minutes_ago(f"DL{i}ABC", minutes)
    rate = activation.qso_rate()
    assert rate["hour"]["previous"] == 3 and rate["hour"]["delta"] == -1
    assert rate["hour"]["trend"] == "down"
    assert rate["ten"]["trend"] == "up" and rate["ten"]["delta"] == 2


def test_qso_rate_can_be_limited_to_one_operator() -> None:
    activation.set_flag("auto_slots", False)
    _qso_minutes_ago("DL1ABC", 5, operator="F4IOZ")
    _qso_minutes_ago("DL2ABC", 5, operator="F5RRO")
    assert activation.qso_rate()["ten"]["qsos"] == 2            # toute la station
    mine = activation.qso_rate("F4IOZ")
    assert mine["ten"]["qsos"] == 1 and mine["operator"] == "F4IOZ"


def test_rate_panel_is_shown_on_the_log_page(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "auth_password", lambda: "secret")
    activation.set_flag("auto_slots", False)
    admin = _private_client()
    assert 'hx-get="/activation/rate"' in admin.get("/activation/log").text
    _qso_minutes_ago("DL1ABC", 3)
    page = admin.get("/activation/rate").text
    assert "Cadence" in page and "dernière heure" in page and "10 dernières min" in page
    assert "act-gauge-bar" in page


def test_rate_panel_needs_the_operator_area() -> None:
    assert TestClient(app, follow_redirects=False).get("/activation/rate").status_code == 303


# ── Spots : la station entendue, pas le spotteur ───────────────────────────


class _FakeResponse:
    def __init__(self, payload=None, text: str = "") -> None:
        self._payload, self.text = payload, text

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


def test_dxwatch_asks_for_the_spotted_station(monkeypatch) -> None:
    """Le filtre est cdx (le DX) : cde donnerait les spots ENVOYÉS par l'indicatif,
    ce qu'une station spéciale ne fait jamais — le panneau restait vide."""
    from app import dx_spots

    seen = {}

    def fake_get(url, params=None, **_kw):
        seen.update(params or {})
        # [spotteur, fréquence, DX, commentaire, horodatage, âge en secondes, …]
        return _FakeResponse({"s": {
            "1": ["ON4ZD", 7188, "TM25TEST", "SES last hours", "1705z 20 Sep", 300, 12, 0],
            "2": ["TM25TEST", 14074, "DL1ABC", "spot envoyé par nous", "1706z 20 Sep", 240, 22, 0],
        }})

    monkeypatch.setattr(dx_spots.httpx, "get", fake_get)
    dx_spots.clear_cache()
    spots = dx_spots.recent_spots("TM25TEST", force=True)
    assert seen.get("cdx") == "TM25TEST" and "cde" not in seen
    assert len(spots) == 1                      # le spot que NOUS avons envoyé ne compte pas
    spot = spots[0]
    assert spot["dx"] == "TM25TEST" and spot["spotter"] == "ON4ZD"
    assert spot["freq_khz"] == 7188 and spot["band"] == "40M"
    assert spot["age_min"] == 5 and spot["comment"] == "SES last hours"
    dx_spots.clear_cache()


def test_old_spots_are_dropped(monkeypatch) -> None:
    from app import dx_spots

    def fake_get(url, params=None, **_kw):
        return _FakeResponse({"s": {
            "1": ["ON4ZD", 7188, "TM25TEST", "hier", "1705z 19 Sep", 30 * 3600, 12, 0],
        }})

    monkeypatch.setattr(dx_spots.httpx, "get", fake_get)
    dx_spots.clear_cache()
    assert dx_spots.recent_spots("TM25TEST", force=True) == []   # « suis-je spotté ? » = maintenant
    dx_spots.clear_cache()


def test_hamqth_fallback_reads_its_own_format(monkeypatch) -> None:
    from app import dx_spots

    when = (datetime.now(timezone.utc) - timedelta(minutes=7)).strftime("%H%M %Y-%m-%d")
    lignes = "\n".join([
        f"TM25TEST^7188.0^ON4ZD^SES last hours^{when}^L^^EU^40M^France^42",
        f"DL1ABC^14074.0^TM25TEST^spot envoyé par nous^{when}^^^EU^20M^Germany^7",
    ])

    def fake_get(url, params=None, **_kw):
        if "dxwatch" in url:
            raise OSError("dxwatch indisponible")
        return _FakeResponse(text=lignes)

    monkeypatch.setattr(dx_spots.httpx, "get", fake_get)
    dx_spots.clear_cache()
    spots = dx_spots.recent_spots("TM25TEST", force=True)
    assert len(spots) == 1 and spots[0]["spotter"] == "ON4ZD"
    assert spots[0]["age_min"] in (6, 7, 8) and spots[0]["band"] == "40M"
    dx_spots.clear_cache()


# ── Périmètre d'un opérateur non administrateur ────────────────────────────


def _member_client(call: str = "F5ABC") -> TestClient:
    """Session d'un opérateur ordinaire (mot de passe individuel, pas admin)."""
    activation.set_flag("per_operator_auth", True)
    activation.set_flag("auto_slots", False)
    client = _op_client()
    assert _login(client, call, PW).status_code == 303
    return client


def test_member_logs_under_their_own_callsign_only() -> None:
    client = _member_client()
    r = client.post("/activation/contacts",
                    data={"call": "DL1ABC", "band": "20M", "mode": "SSB",
                          "operator": "F5RRO", "now": "1"})      # tentative au nom d'un autre
    assert r.status_code == 200
    assert [q["operator_call"] for q in activation.list_contacts()] == ["F5ABC"]


def test_member_cannot_switch_to_another_operator() -> None:
    client = _member_client()
    client.post("/activation/whoami", data={"operator": "F5RRO", "next": "/activation/log"})
    page = client.get("/activation/log").text
    assert "F5ABC" in page and "F5RRO" not in page
    assert '<select name="operator"' not in page        # plus de choix : l'indicatif est imposé


def test_member_cannot_touch_another_operators_qso() -> None:
    activation.set_flag("auto_slots", False)
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F5RRO")
    other = activation.list_contacts()[0]["id"]
    client = _member_client()
    r = client.post(f"/activation/contacts/{other}/delete")
    assert "autre opérateur" in r.text and len(activation.list_contacts()) == 1
    assert client.get(f"/activation/contacts/{other}/edit").status_code == 303
    r = client.post(f"/activation/contacts/{other}",
                    data={"call": "DL9ZZZ", "band": "20M", "mode": "SSB", "operator": "F5ABC"})
    assert r.status_code == 303 and activation.list_contacts()[0]["call"] == "DL1ABC"
    # Son propre QSO, en revanche, lui appartient.
    client.post("/activation/contacts",
                data={"call": "ON4ZZ", "band": "20M", "mode": "SSB", "now": "1"})
    mine = [q for q in activation.list_contacts() if q["operator_call"] == "F5ABC"][0]["id"]
    assert "autre opérateur" not in client.post(f"/activation/contacts/{mine}/delete").text
    assert [q["call"] for q in activation.list_contacts()] == ["DL1ABC"]


def test_member_exports_only_their_own_qso() -> None:
    activation.set_flag("auto_slots", False)
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F5RRO")
    client = _member_client()
    client.post("/activation/contacts",
                data={"call": "ON4ZZ", "band": "20M", "mode": "SSB", "now": "1"})
    adif = client.get("/activation/export.adi").text
    assert "ON4ZZ" in adif and "DL1ABC" not in adif
    csv_body = client.get("/activation/export.csv").text
    assert "ON4ZZ" in csv_body and "DL1ABC" not in csv_body
    page = client.get("/activation/adif").text
    assert "ON4ZZ" in page and "DL1ABC" not in page
    # Même en cochant l'id du QSO d'un autre dans l'export d'une sélection.
    other = [q for q in activation.list_contacts() if q["operator_call"] == "F5RRO"][0]["id"]
    r = client.post("/activation/export-selection.adi", data={"ids": [other]})
    assert r.status_code == 303 and "err=empty" in r.headers["location"]


def test_member_books_slots_under_their_own_callsign_only() -> None:
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M"
    autre = activation.add_slot("F5RRO", (now + timedelta(hours=3)).strftime(fmt),
                                (now + timedelta(hours=4)).strftime(fmt), "20M", "SSB")
    client = _member_client()
    client.post("/activation/slots",
                data={"operator": "F5RRO", "start": (now + timedelta(hours=5)).strftime(fmt),
                      "end": (now + timedelta(hours=6)).strftime(fmt), "band": "40M", "mode": "CW"})
    nouveau = [s for s in activation.list_slots() if s["band"] == "40M"]
    assert nouveau and nouveau[0]["operator_call"] == "F5ABC"    # réservé pour lui, pas pour F5RRO
    # Le créneau d'un autre reste intouchable.
    assert client.get(f"/activation/slots/{autre}/edit").status_code == 303
    client.post(f"/activation/slots/{autre}/delete")
    assert activation.get_slot(autre) is not None


def test_admin_operator_keeps_the_full_scope() -> None:
    activation.set_flag("per_operator_auth", True)
    activation.add_operator("F5BOS", "Chef")
    activation.set_operator_admin("F5BOS", True)
    client = _op_client()
    assert _login(client, "F5BOS", PW).status_code == 303
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F5RRO")
    page = client.get("/activation/log").text
    assert '<select name="operator"' in page                     # il choisit qui est au micro
    assert "DL1ABC" in client.get("/activation/export.adi").text  # et exporte tout le log


def _shared_client(call: str = "F5ABC") -> TestClient:
    """Session ouverte avec le MOT DE PASSE COMMUN, sous un indicatif donné."""
    activation.set_flag("per_operator_auth", False)
    activation.set_flag("auto_slots", False)
    activation.set_operator_password("oppass")
    client = _op_client()
    assert client.post("/activation/login",
                       data={"callsign": call, "password": "oppass"}).status_code == 303
    return client


def test_shared_password_also_keeps_each_operator_on_their_callsign() -> None:
    client = _shared_client("F5ABC")
    client.post("/activation/contacts",
                data={"call": "DL1ABC", "band": "20M", "mode": "SSB",
                      "operator": "F5RRO", "now": "1"})
    assert [q["operator_call"] for q in activation.list_contacts()] == ["F5ABC"]
    page = client.get("/activation/log").text
    assert "F5ABC" in page and '<select name="operator"' not in page


def test_shared_password_admin_flag_frees_the_choice_but_not_the_settings() -> None:
    """Coché « administrateur », on choisit de nouveau qui est au micro ; les
    Réglages restent fermés tant qu'il n'y a pas de mot de passe personnel."""
    activation.add_operator("F5BOS", "Chef")
    activation.set_operator_admin("F5BOS", True)
    client = _shared_client("F5BOS")
    page = client.get("/activation/log").text
    assert '<select name="operator"' in page
    client.post("/activation/contacts",
                data={"call": "DL1ABC", "band": "20M", "mode": "SSB",
                      "operator": "F5RRO", "now": "1"})
    assert [q["operator_call"] for q in activation.list_contacts()] == ["F5RRO"]
    assert client.get("/activation/settings").status_code == 303   # mot de passe commun : non


# ── Heure locale : celle du visiteur ───────────────────────────────────────


def test_visitor_timezone_drives_the_local_display() -> None:
    _public()
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ",
                           qso_date="20260921", time_on="1200")
    activation.set_flag("show_contacts", True)
    heures = {}
    for tz in ("Europe/Paris", "America/New_York", "Asia/Tokyo"):
        client = TestClient(app)
        client.cookies.set("tm_tzname", tz)
        page = client.get("/tm25test").text
        heures[tz] = re.findall(r"\b\d{2}:\d{2}\b", page)[0]
    assert heures["Europe/Paris"] == "14:00"        # 12:00 UTC en heure d'été
    assert heures["America/New_York"] == "08:00"
    assert heures["Asia/Tokyo"] == "21:00"
    # L'étiquette nomme le fuseau du visiteur.
    client = TestClient(app)
    client.cookies.set("tm_tzname", "America/New_York")
    assert "Locale (New York)" in client.get("/tm25test").text


def test_unknown_or_hostile_timezone_falls_back_to_the_station() -> None:
    _public()
    for bogus in ("Mars/Olympus", "../../etc/passwd", "x" * 200, ""):
        client = TestClient(app)
        client.cookies.set("tm_tzname", bogus)
        assert "Locale (Paris)" in client.get("/tm25test").text
    assert activation.tz_label("local") == "Paris" and activation.tz_label("utc") == "UTC"
    assert activation.valid_tz("Europe/Brussels") and not activation.valid_tz("Mars/Olympus")


def test_utc_mode_still_wins_over_the_visitor_timezone() -> None:
    _public()
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4IOZ",
                           qso_date="20260921", time_on="1200")
    activation.set_flag("show_contacts", True)
    client = TestClient(app)
    client.cookies.set("tm_tzname", "Asia/Tokyo")
    client.cookies.set("tm_tz", "utc")
    page = client.get("/tm25test").text
    assert re.findall(r"\b\d{2}:\d{2}\b", page)[0] == "12:00"
    assert "Locale (Tokyo)" in page          # la bascule propose son heure à lui


def test_typed_times_are_read_in_the_visitor_timezone() -> None:
    """Un créneau saisi à 20:00 à New York n'est pas 20:00 à Paris."""
    assert activation.input_to_utc_iso("2026-09-21T20:00", "America/New_York") == "2026-09-22T00:00"
    assert activation.input_to_utc_iso("2026-09-21T20:00", "local") == "2026-09-21T18:00"
    assert activation.input_to_utc_iso("2026-09-21T20:00", "utc") == "2026-09-21T20:00"
