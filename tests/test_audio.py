"""QSO audio (optional): segments sent by the operator's browser, excerpt of
each QSO, retention and quota, access limited to the operators area."""

from __future__ import annotations

import calendar
import time

import pytest
from fastapi.testclient import TestClient

from app import activation
from app.main import app
from app.routers import activation as activation_router

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 200          # EBML header: enough for the check
OGG = b"OggS" + b"\x00" * 200


def now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def audio_on():
    activation.set_audio_options({"enabled": "1", "retention_days": "30", "quota_mb": "100",
                                  "before": "90", "after": "20"})


def _segment(op: str = "F4ABC", start: int | None = None, dur: int = 20_000, data: bytes = WEBM,
             mime: str = "audio/webm;codecs=opus", skew: int = 0) -> int:
    start = now_ms() - dur if start is None else start
    return activation.add_audio_segment(data, mime, op, start + skew, dur, now_ms() + skew)


def test_disabled_by_default() -> None:
    assert not activation.audio_enabled()
    with pytest.raises(ValueError):
        _segment()


def test_segment_stored_on_the_server_clock(audio_on) -> None:
    # The browser's clock is an hour late: the segment is put back at the right time.
    before = now_ms()
    seg = _segment(skew=-3_600_000)
    path, mime = activation.audio_segment(seg)
    assert path.read_bytes() == WEBM and mime == "audio/webm"
    assert activation.AUDIO_DIR.resolve() in path.resolve().parents
    with activation.conn() as c:
        start, end = c.execute("SELECT start_ms, end_ms FROM audio_segments WHERE id=?", (seg,)).fetchone()
    assert abs(start - (before - 20_000)) < 2_000 and end - start == 20_000
    assert _segment(data=OGG, mime="audio/ogg") > seg


@pytest.mark.parametrize("kwargs", [
    {"data": b"RIFF....WAVEfmt "},                    # not Opus
    {"mime": "audio/wav"},
    {"data": WEBM + b"\x00" * activation.AUDIO_SEGMENT_MAX_BYTES},
    {"dur": 120_000},
    {"dur": 10},
    {"op": "not a call"},
    {"start": now_ms() - 3_600_000},                  # sent an hour late
    {"start": now_ms() + 60_000},                     # in the future
])
def test_bad_segments_refused(audio_on, kwargs) -> None:
    with pytest.raises(ValueError):
        _segment(**kwargs)
    assert activation.audio_usage()["segments"] == 0


def test_qso_instant() -> None:
    q = {"qso_date": "20260927", "time_on": "1746", "created_at": 0}
    minute = calendar.timegm(time.strptime("202609271746", "%Y%m%d%H%M"))
    assert activation.qso_instant_ms(q) == minute * 1000 + 30_000       # middle of the minute
    q["created_at"] = minute + 42                                         # logged "now": the second
    assert activation.qso_instant_ms(q) == (minute + 42) * 1000
    q["created_at"] = minute + 3600                                       # edited later: ignored
    assert activation.qso_instant_ms(q) == minute * 1000 + 30_000


def test_clip_of_a_qso(audio_on) -> None:
    t = now_ms()
    mine = [_segment("F4ABC", t - 60_000, 20_000), _segment("F4ABC", t - 40_000, 20_000),
            _segment("F4ABC", t - 20_000, 20_000)]
    _segment("F5XYZ", t - 20_000, 20_000)                          # another operator's browser
    _segment("F4ABC", t - 120_000, 20_000)                        # ends before the excerpt
    qso = activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4ABC")
    clip = activation.audio_clip(qso)
    assert clip["call"] == "DL1ABC" and clip["operator"] == "F4ABC"
    assert clip["to_ms"] - clip["at_ms"] == 20_000 and clip["at_ms"] - clip["from_ms"] == 90_000
    assert [s["id"] for s in clip["segments"]] == mine
    # QSO of an operator who recorded nothing: the station's other recording.
    other = activation.add_contact(call="G4ABC", band="20M", mode="SSB", operator_call="F1QQQ")
    assert len(activation.audio_clip(other)["segments"]) == 4


def test_retention(audio_on) -> None:
    old = _segment(start=now_ms() - 20_000)
    with activation.conn() as c:                      # recorded 40 days ago
        c.execute("UPDATE audio_segments SET start_ms=start_ms-?, end_ms=end_ms-? WHERE id=?",
                  (40 * 86_400_000, 40 * 86_400_000, old))
    path = activation.audio_segment(old)[0]
    kept = _segment()
    assert activation.audio_segment(old) is None and not path.exists()
    assert activation.audio_segment(kept) is not None


def test_quota_keeps_the_newest(audio_on, monkeypatch) -> None:
    ids = [_segment(start=now_ms() - 80_000 + i * 20_000) for i in range(3)]
    real = activation.get_audio_options
    monkeypatch.setattr(activation, "get_audio_options", lambda: {**real(), "quota_mb": 1})
    # 1 MB minus 50 bytes: with it, the three older ones no longer fit.
    big = activation.add_audio_segment(WEBM[:4] + b"\x00" * (1024 * 1024 - 54), "audio/webm", "F4ABC",
                                       now_ms() - 20_000, 20_000, now_ms())
    assert activation.audio_segment(big) is not None
    assert all(activation.audio_segment(i) is None for i in ids)      # the oldest went


def test_clear_audio(audio_on) -> None:
    seg = _segment()
    path = activation.audio_segment(seg)[0]
    activation.clear_audio()
    assert not path.exists() and activation.audio_usage()["segments"] == 0


# ── HTTP ───────────────────────────────────────────────────────────────────


@pytest.fixture
def operator(monkeypatch) -> TestClient:
    monkeypatch.setattr(activation, "operator_password", lambda: "commun")
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(activation.OP_COOKIE, activation.make_op_token("F4ABC"))
    return client


def test_http_upload_listen_and_access(audio_on, operator) -> None:
    anonymous = TestClient(app, follow_redirects=False)
    params = {"start": now_ms() - 20_000, "dur": 20_000, "now": now_ms()}
    assert anonymous.post("/activation/audio", params=params, content=WEBM,
                          headers={"Content-Type": "audio/webm"}).status_code == 303
    r = operator.post("/activation/audio", params=params, content=WEBM, headers={"Content-Type": "audio/webm"})
    assert r.status_code == 200, r.text
    seg = r.json()["id"]
    bad = operator.post("/activation/audio", params=params, content=b"nope", headers={"Content-Type": "audio/webm"})
    assert bad.status_code == 400 and "format" in bad.json()["error"]

    qso = activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4ABC")
    clip = operator.get(f"/activation/contacts/{qso}/audio").json()
    assert [s["id"] for s in clip["segments"]] == [seg]
    audio = operator.get(clip["segments"][0]["url"])
    assert audio.status_code == 200 and audio.content == WEBM and audio.headers["content-type"] == "audio/webm"
    # Internal listening only.
    assert anonymous.get(clip["segments"][0]["url"]).status_code == 303
    assert anonymous.get(f"/activation/contacts/{qso}/audio").status_code == 303
    # The settings are the superadmin's.
    assert operator.post("/activation/settings/audio", data={"enabled": ""}).status_code == 303
    assert activation.audio_enabled()


def test_log_page_offers_recording_only_when_enabled(operator) -> None:
    page = operator.get("/activation/log").text
    assert 'id="act-audio"' not in page and "activation-audio.js" not in page
    activation.set_audio_options({"enabled": "1"})
    page = operator.get("/activation/log").text
    assert 'id="act-audio"' in page and "activation-audio.js" in page
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4ABC")
    assert 'data-qso="' in operator.get("/activation/log").text


def test_upload_refused_when_disabled(operator) -> None:
    r = operator.post("/activation/audio", params={"start": now_ms() - 20_000, "dur": 20_000, "now": now_ms()},
                      content=WEBM, headers={"Content-Type": "audio/webm"})
    assert r.status_code == 400


# ── Public listening (option) ──────────────────────────────────────────────


@pytest.fixture
def public_station():
    st = activation.update_station(activation.callsign(), public=1)
    return st


def _qso_with_audio(op: str = "F4ABC", call: str = "DL1ABC") -> tuple[int, int]:
    seg = _segment(op)
    return activation.add_contact(call=call, band="20M", mode="SSB", operator_call=op), seg


def test_public_listening_off_by_default(audio_on, public_station) -> None:
    qso, _seg = _qso_with_audio()
    client = TestClient(app)
    assert client.get(f"/{public_station['slug']}/qso/{qso}/audio").status_code == 404
    page = client.get(f"/{public_station['slug']}?call=DL1ABC").text
    assert "data-audio=" not in page and "activation-audio.js" not in page


def test_public_listening(audio_on, public_station) -> None:
    activation.set_audio_options({**activation.get_audio_options(), "public": "1"})
    slug = public_station["slug"]
    qso, seg = _qso_with_audio()
    silent = activation.add_contact(call="DL1ABC", band="40M", mode="CW", operator_call="F4ABC",
                                    qso_date="20250101", time_on="1200")     # no recording back then
    client = TestClient(app)
    page = client.get(f"/{slug}?call=DL1ABC").text
    assert f'data-audio="/{slug}/qso/{qso}/audio"' in page and f"/qso/{silent}/audio" not in page
    assert "activation-audio.js" in page
    clip = client.get(f"/{slug}/qso/{qso}/audio")
    assert clip.status_code == 200 and clip.headers["x-robots-tag"].startswith("noindex")
    url = clip.json()["segments"][0]["url"]
    assert url == f"/{slug}/qso/{qso}/audio/{seg}"
    assert client.get(url).content == WEBM
    assert client.get(f"/{slug}/qso/{silent}/audio").status_code == 404


def test_public_serves_only_the_segments_of_the_excerpt(audio_on, public_station) -> None:
    activation.set_audio_options({**activation.get_audio_options(), "public": "1"})
    slug = public_station["slug"]
    elsewhere = _segment(start=now_ms() - 600_000)         # 10 min earlier: chatter between QSOs
    qso, _seg = _qso_with_audio()
    client = TestClient(app)
    assert client.get(f"/{slug}/qso/{qso}/audio/{elsewhere}").status_code == 404
    # Another special callsign's page does not serve this station's QSOs.
    other = activation.create_station("TM9ZZZ", public=1)
    assert client.get(f"/{other['slug']}/qso/{qso}/audio").status_code == 404


def test_admin_hides_a_qso_from_the_public(audio_on, public_station, operator, monkeypatch) -> None:
    activation.set_audio_options({**activation.get_audio_options(), "public": "1"})
    slug = public_station["slug"]
    qso, _seg = _qso_with_audio()
    # A plain operator cannot.
    assert operator.post(f"/activation/contacts/{qso}/audio-public", data={"hidden": "1"}).status_code == 403
    assert operator.get(f"/activation/contacts/{qso}/audio").json()["can_hide"] is False
    monkeypatch.setattr(activation_router, "_is_admin", lambda request: True)
    assert operator.get(f"/activation/contacts/{qso}/audio").json()["can_hide"] is True
    r = operator.post(f"/activation/contacts/{qso}/audio-public", data={"hidden": "1"})
    assert r.json() == {"hidden": True}
    public = TestClient(app)
    assert public.get(f"/{slug}/qso/{qso}/audio").status_code == 404
    assert "data-audio=" not in public.get(f"/{slug}?call=DL1ABC").text
    internal = operator.get(f"/activation/contacts/{qso}/audio").json()     # still there inside
    assert internal["hidden"] is True and internal["segments"]
    operator.post(f"/activation/contacts/{qso}/audio-public", data={"hidden": "0"})
    assert public.get(f"/{slug}/qso/{qso}/audio").status_code == 200


def test_public_listening_is_rate_limited(audio_on, public_station, monkeypatch) -> None:
    activation.set_audio_options({**activation.get_audio_options(), "public": "1"})
    monkeypatch.setattr(activation_router, "PUBLIC_AUDIO_PER_MINUTE", 2)
    qso, _seg = _qso_with_audio()
    client = TestClient(app)
    codes = [client.get(f"/{public_station['slug']}/qso/{qso}/audio").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
