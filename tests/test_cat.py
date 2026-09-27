"""CAT of the log page (static/js/activation-cat.js).

- Static checks: the script only ever sends read commands.
- Browser checks (skipped without Playwright + Chromium): a fake Web Serial
  port plays an IC-9700 (CI-V with USB echo and transceive) or Thetis
  emulating a TS-2000, and the log form must follow the radio.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "activation-cat.js").read_text(encoding="utf-8")


def test_only_read_commands_are_whitelisted() -> None:
    icom = re.search(r"var READ_ICOM = \[([^\]]*)\]", JS).group(1)
    assert [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", icom)] == [0x03, 0x04]
    kenwood = re.search(r"var READ_KENWOOD = \[([^\]]*)\]", JS).group(1)
    assert re.findall(r"'([^']*)'", kenwood) == ["FA;", "MD;", "ID;"]


def test_every_write_goes_through_the_whitelist() -> None:
    calls = re.findall(r"\bawait send\((.+)\);", JS)
    assert calls, "send() is no longer used?"
    assert all(c.startswith(("civCommand(", "kwCommand(")) for c in calls), calls
    assert JS.count("writer.write(") == 1                     # only inside send()
    assert "throw new Error('CAT: write command refused')" in JS


def test_log_page_loads_the_cat_script() -> None:
    html = (ROOT / "templates" / "activation" / "log.html").read_text(encoding="utf-8")
    assert 'id="act-cat"' in html and "activation-cat.js" in html and "act-cat-refresh" in html


# ── Browser ────────────────────────────────────────────────────────────────

PAGE = """<!doctype html><html><body>
<div class="act-card act-op-select"><div id="act-cat"></div></div>
<form class="act-log-form">
  <select name="band">%s</select>
  <select name="mode">%s</select>
  <input name="freq">
</form>
<script>window.__changes = [];
  document.querySelectorAll('select').forEach(function (s) {
    s.addEventListener('change', function () { window.__changes.push(s.name + '=' + s.value); });
  });</script>
<script src="/cat.js"></script>
</body></html>"""

BANDS = ["160M", "80M", "60M", "40M", "30M", "20M", "17M", "15M", "12M", "10M", "6M", "4M", "2M", "70CM", "23CM"]
MODES = ["SSB", "CW", "FT8", "FT4", "RTTY", "PSK31", "FM", "AM", "SSTV", "DIGI"]

# Fake Web Serial port. `kind`: "icom" (IC-9700 at A2h) or "kenwood" (Thetis).
FAKE_SERIAL = """
(() => {
  const written = [];
  let ctrl = null, hz = 145837500, mode = 0x05, kmode = '9';
  const bcd = (f) => { const s = String(f).padStart(10, '0'), b = [];
    for (let i = 0; i < 5; i++) b.push((+s[8 - 2 * i] << 4) | +s[9 - 2 * i]); return b; };
  const push = (bytes) => ctrl && ctrl.enqueue(new Uint8Array(bytes));
  const pushText = (s) => ctrl && ctrl.enqueue(new TextEncoder().encode(s));
  const port = {
    getInfo: () => ({ usbVendorId: 0x10C4, usbProductId: 0xEA60 }),
    open: async () => {},
    close: async () => {},
    readable: new ReadableStream({ start(c) { ctrl = c; } }),
    writable: new WritableStream({ write(chunk) {
      const b = Array.from(chunk); written.push(b);
      if (window.__kind === 'icom') {
        push(b);                                            // CI-V USB echo
        if (b[2] !== 0xA2) return;                          // another address: silence
        if (b[4] === 0x03) push([0xFE, 0xFE, 0xE0, 0xA2, 0x03, ...bcd(hz), 0xFD]);
        if (b[4] === 0x04) push([0xFE, 0xFE, 0xE0, 0xA2, 0x04, mode, 0x01, 0xFD]);
      } else {
        const s = new TextDecoder().decode(chunk);
        if (s === 'FA;') pushText('FA' + String(hz).padStart(11, '0') + ';');
        if (s === 'MD;') pushText('MD' + kmode + ';');
        if (s === 'ID;') pushText('ID019;');
      }
    } }),
  };
  window.__radio = {
    written,
    transceive: (f, m) => { hz = f; mode = m;               // VFO turned on the radio
      push([0xFE, 0xFE, 0x00, 0xA2, 0x00, ...bcd(f), 0xFD]);
      push([0xFE, 0xFE, 0x00, 0xA2, 0x01, m, 0x01, 0xFD]); },
    kenwood: (f, m) => { hz = f; kmode = m; },
  };
  Object.defineProperty(navigator, 'serial', { value: {
    requestPort: async () => port, getPorts: async () => [], addEventListener: () => {},
  } });
})();
"""


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — no browser installed (Raspberry Pi…)
            pytest.skip(f"Chromium unavailable: {exc}")
        yield b
        b.close()


def _page(browser, kind: str, start_mode: str = "SSB", setup: str = ""):
    options = lambda xs, sel="": "".join(f'<option{" selected" if x == sel else ""}>{x}</option>' for x in xs)  # noqa: E731
    html = PAGE % (options(BANDS, "20M"), options(MODES, start_mode))
    page = browser.new_page()
    # HTTPS origin: Web Serial only exists in a secure context.
    page.route("https://cat.test/", lambda r: r.fulfill(body=html, content_type="text/html"))
    page.route("https://cat.test/cat.js", lambda r: r.fulfill(body=JS, content_type="text/javascript"))
    page.add_init_script(f"window.__kind = '{kind}';" + FAKE_SERIAL)
    page.goto("https://cat.test/")
    if kind == "kenwood":
        page.click(".act-cat-cfg summary")
        page.select_option("select[data-k=proto]", "kenwood")
    if setup:
        page.evaluate(setup)                                   # radio state before connecting
    page.click(".act-cat-btn")
    return page


def _form(page) -> tuple[str, str, str]:
    return (page.input_value("input[name=freq]"), page.input_value("select[name=band]"),
            page.input_value("select[name=mode]"))


def test_icom_ic9700(browser) -> None:
    page = _page(browser, "icom")
    page.wait_for_function("document.querySelector('input[name=freq]').value !== ''", timeout=15000)
    assert _form(page) == ("145.8375", "2M", "FM")
    assert "IC-9700" in page.inner_text(".act-cat-status")
    # VFO turned on the radio: pushed by CI-V transceive, no polling needed.
    page.evaluate("__radio.transceive(435100000, 0x01)")
    page.wait_for_function("document.querySelector('input[name=freq]').value === '435.100'")
    assert _form(page) == ("435.100", "70CM", "SSB")
    assert "band=70CM" in page.evaluate("__changes")        # spots / slot lock warned
    # Only read commands ever reached the radio (03 / 04), whatever the address probed.
    frames = page.evaluate("__radio.written")
    assert frames and all(f[:2] == [0xFE, 0xFE] and f[3] == 0xE0 and f[4] in (0x03, 0x04) and len(f) == 6
                          for f in frames)
    page.close()


def test_kenwood_thetis_keeps_the_digital_mode(browser) -> None:
    page = _page(browser, "kenwood", start_mode="FT8")
    page.wait_for_function("document.querySelector('input[name=freq]').value !== ''", timeout=15000)
    assert _form(page) == ("145.8375", "2M", "FT8")          # DIGU: FT8 chosen in the form is kept
    page.evaluate("__radio.kenwood(14074000, '9')")          # still DIGU, other band
    page.wait_for_function("document.querySelector('input[name=freq]').value === '14.074'")
    assert _form(page) == ("14.074", "20M", "FT8")
    page.evaluate("__radio.kenwood(14200000, '2')")          # USB = voice for Kenwood/Thetis
    page.wait_for_function("document.querySelector('select[name=mode]').value === 'SSB'")
    assert _form(page) == ("14.200", "20M", "SSB")
    page.evaluate("__radio.kenwood(7012000, '3')")           # CW
    page.wait_for_function("document.querySelector('select[name=mode]').value === 'CW'")
    assert _form(page) == ("7.012", "40M", "CW")
    assert "TS-2000" in page.inner_text(".act-cat-status")
    sent = {bytes(f).decode() for f in page.evaluate("__radio.written")}
    assert sent <= {"FA;", "MD;", "ID;"}
    page.close()


def test_voice_mode_on_usb_is_ssb(browser) -> None:
    """DIGU first (→ DIGI), then the operator goes back to USB voice: SSB, not DIGI."""
    page = _page(browser, "kenwood", start_mode="CW")
    page.wait_for_function("document.querySelector('select[name=mode]').value === 'DIGI'", timeout=15000)
    page.evaluate("__radio.kenwood(14200000, '2')")
    page.wait_for_function("document.querySelector('select[name=mode]').value === 'SSB'", timeout=15000)
    assert _form(page) == ("14.200", "20M", "SSB")
    page.close()


def test_icom_usb_keeps_ft8(browser) -> None:
    """IC-9700 in USB-D reports USB: the FT8 chosen in the form stays."""
    page = _page(browser, "icom", start_mode="FT8", setup="__radio.transceive(144174000, 0x01)")
    page.wait_for_function("document.querySelector('input[name=freq]').value === '144.174'", timeout=15000)
    assert _form(page) == ("144.174", "2M", "FT8")
    page.close()
