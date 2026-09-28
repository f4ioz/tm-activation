"""One audio file per worked callsign (app/opus_cut.py + activation export).

tests/fixtures/tone-3s.webm is a real recording from Chromium's MediaRecorder
(3 s of a 440 Hz sine, Opus 24 kbit/s): the same container as the log page.
"""

from __future__ import annotations

import base64
import io
import struct
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import activation, opus_cut
from app.main import app
from app.routers import activation as activation_router

TONE = (Path(__file__).parent / "fixtures" / "tone-3s.webm").read_bytes()


def duration_ms(ogg_or_webm: bytes) -> float:
    return sum(map(opus_cut.packet_samples, opus_cut.read_packets(ogg_or_webm)[1])) * 1000 / opus_cut.RATE


# ── Opus / containers ──────────────────────────────────────────────────────


@pytest.mark.parametrize("packet, samples", [
    (bytes([31 << 3 | 0]), 960),            # CELT 20 ms, one frame
    (bytes([31 << 3 | 1]), 1920),           # two frames
    (bytes([31 << 3 | 3, 3]), 2880),        # code 3: frame count in the 2nd byte
    (bytes([1 << 3]), 960),                 # SILK 20 ms
    (bytes([3 << 3]), 2880),                # SILK 60 ms
    (bytes([13 << 3]), 960),                # hybrid 20 ms
    (bytes([16 << 3]), 120),                # CELT 2.5 ms
    (b"", 0),
])
def test_packet_samples(packet, samples) -> None:
    assert opus_cut.packet_samples(packet) == samples


def test_reads_a_real_chrome_recording() -> None:
    head, packets = opus_cut.read_packets(TONE)
    assert head.startswith(b"OpusHead") and len(packets) > 40
    assert 2800 <= duration_ms(TONE) <= 3100


def test_cut_one_window() -> None:
    out = opus_cut.cut([opus_cut.Segment(1_000_000, TONE)], [(1_000_500, 1_002_000)], title="DL1ABC")
    pages = opus_cut.ogg_pages(out)                       # every CRC checked
    assert pages[0]["type"] == 0x02 and pages[0]["body"].startswith(b"OpusHead")
    assert pages[1]["body"].startswith(b"OpusTags") and b"TITLE=DL1ABC" in pages[1]["body"]
    assert pages[-1]["type"] == 0x04
    assert abs(duration_ms(out) - 1500) <= 60              # one packet of precision
    head = pages[0]["body"]
    pre_skip = struct.unpack_from("<H", head, 10)[0]
    assert pages[-1]["granule"] == pre_skip + duration_ms(out) * 48


def test_overlapping_recordings_are_not_doubled() -> None:
    # The browser starts the next recorder a little before stopping the previous one.
    total = duration_ms(TONE)
    segs = [opus_cut.Segment(0, TONE), opus_cut.Segment(int(total) - 100, TONE)]
    out = opus_cut.cut(segs, [(0, 10_000)])
    assert 2 * total - 200 <= duration_ms(out) <= 2 * total - 90


def test_windows_are_merged_and_ordered() -> None:
    segs = [opus_cut.Segment(0, TONE)]
    out = opus_cut.cut(segs, [(1500, 2500), (0, 1000), (800, 1600)])
    assert abs(duration_ms(out) - 2500) <= 120
    assert opus_cut.cut(segs, [(10_000, 12_000)]) is None   # nothing recorded there


def test_ogg_input_and_damaged_segment() -> None:
    ogg = opus_cut.cut([opus_cut.Segment(0, TONE)], [(0, 3000)])
    again = opus_cut.cut([opus_cut.Segment(0, ogg), opus_cut.Segment(5000, b"OggS broken")], [(0, 3000)])
    assert abs(duration_ms(again) - duration_ms(ogg)) <= 1
    broken = bytearray(ogg)
    broken[40] ^= 0xFF
    with pytest.raises(ValueError):
        opus_cut.ogg_pages(bytes(broken))                  # bad CRC detected


# ── Export per callsign ────────────────────────────────────────────────────


@pytest.fixture
def recorded():
    """Audio on, one recording made right now and a QSO with DL1ABC in it."""
    activation.set_audio_options({"enabled": "1"})
    dur = int(duration_ms(TONE))
    now = int(time.time() * 1000)
    activation.add_audio_segment(TONE, "audio/webm", "F4ABC", now - dur, dur, now)
    activation.add_contact(call="DL1ABC", band="20M", mode="SSB", operator_call="F4ABC")
    activation.add_contact(call="G4XYZ", band="20M", mode="SSB", operator_call="F4ABC",
                           qso_date="20250101", time_on="1200")      # no recording back then


def test_audio_for_call(recorded) -> None:
    out = activation.audio_for_call("dl1abc", before_s=2, after_s=0)
    assert out and 1800 <= duration_ms(out) <= 2100         # 2 s before the QSO
    assert activation.audio_for_call("G4XYZ") is None
    assert activation.audio_for_call("F9NONE") is None
    assert activation.export_file_name("EA8/DL1ABC") == f"{activation.slugify_call(activation.callsign())}-EA8-DL1ABC.ogg"


def test_zip_has_one_file_per_callsign_with_audio(recorded) -> None:
    path, count = activation.audio_export_zip(before_s=5, after_s=5)
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == [activation.export_file_name("DL1ABC")] and count == 1
        opus_cut.ogg_pages(zf.read(zf.namelist()[0]))
    assert activation.AUDIO_EXPORT_DIR.resolve() in path.resolve().parents


@pytest.fixture
def operator(monkeypatch) -> TestClient:
    monkeypatch.setattr(activation, "operator_password", lambda: "commun")
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(activation.OP_COOKIE, activation.make_op_token("F4ABC"))
    return client


def test_http_export(recorded, operator, monkeypatch) -> None:
    r = operator.get("/activation/audio-export", params={"call": "DL1ABC", "before": "3", "after": "1"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/ogg"
    assert 'filename="' in r.headers["content-disposition"] and "DL1ABC.ogg" in r.headers["content-disposition"]
    assert 1500 <= duration_ms(r.content) <= 3100
    none = operator.get("/activation/audio-export", params={"call": "G4XYZ"})
    assert none.status_code == 303 and "err=noaudio" in none.headers["location"]
    # The zip of the whole activation: admins only.
    assert operator.get("/activation/audio-export").headers["location"].startswith("/login")
    monkeypatch.setattr(activation_router, "_is_admin", lambda request: True)
    z = operator.get("/activation/audio-export")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(z.content)).namelist() == [activation.export_file_name("DL1ABC")]
    assert not list(activation.AUDIO_EXPORT_DIR.glob("*.zip"))   # temporary zip deleted once sent
    assert TestClient(app, follow_redirects=False).get(
        "/activation/audio-export", params={"call": "DL1ABC"}).status_code == 303   # not logged in


def test_adif_page_offers_the_export(operator) -> None:
    assert 'id="audio"' not in operator.get("/activation/adif").text
    activation.set_audio_options({"enabled": "1"})
    assert 'action="/activation/audio-export"' in operator.get("/activation/adif").text


# ── A real decoder must accept the file (skipped without Chromium) ─────────


def test_chromium_decodes_the_cut() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    out = opus_cut.cut([opus_cut.Segment(0, TONE)], [(500, 2000)])
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content("<p>decode</p>")
        info = page.evaluate("""async (b64) => {
          const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const buf = await new OfflineAudioContext(1, 48000, 48000).decodeAudioData(bin.buffer);
          const d = buf.getChannelData(0); let zc = 0;
          for (let i = 1; i < d.length; i++) if ((d[i - 1] < 0) !== (d[i] < 0)) zc++;
          return {duration: buf.duration, freq: zc / 2 / buf.duration};
        }""", base64.b64encode(out).decode())
        browser.close()
    assert abs(info["duration"] - 1.5) < 0.07
    assert 430 <= info["freq"] <= 450                       # still the 440 Hz sine
