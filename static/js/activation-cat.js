/* CAT on the log page: frequency, band and mode read from the radio plugged
 * into the operator's computer, through Web Serial (Chrome / Edge, page
 * served over HTTPS). No driver and no Hamlib: two small protocols.
 *
 *  - Icom CI-V (IC-9700, IC-7300, IC-705…): commands 03 (frequency) and
 *    04 (mode); the radio also broadcasts every VFO change (CI-V transceive).
 *  - Kenwood (TS-2000, and the SDR programs that emulate one for the CAT:
 *    Thetis / SparkSDR for a Brick2 / ANAN): FA; and MD;.
 *
 * READ ONLY: every byte sent goes through send(), which only lets through the
 * read commands of READ_ICOM / READ_KENWOOD. Nothing here can change the
 * frequency or key the transmitter.
 *
 * window.ActCat exposes the pure functions (parsing, band plan, modes) for
 * the tests.
 */
(function () {
  'use strict';

  // ── Pure functions ─────────────────────────────────────────────────────

  // Bands of the log (activation.BANDS), edges in MHz.
  var BANDS = [
    [1.8, 2.0, '160M'], [3.5, 4.0, '80M'], [5.25, 5.45, '60M'], [7.0, 7.3, '40M'],
    [10.1, 10.15, '30M'], [14.0, 14.35, '20M'], [18.068, 18.168, '17M'], [21.0, 21.45, '15M'],
    [24.89, 24.99, '12M'], [28.0, 29.7, '10M'], [50, 54, '6M'], [70, 71, '4M'],
    [144, 148, '2M'], [420, 450, '70CM'], [1240, 1300, '23CM']
  ];
  var DIGITAL = ['FT8', 'FT4', 'RTTY', 'PSK31', 'SSTV', 'DIGI'];

  function bandOf(hz) {
    var mhz = hz / 1e6;
    for (var i = 0; i < BANDS.length; i++) {
      if (mhz >= BANDS[i][0] && mhz <= BANDS[i][1]) return BANDS[i][2];
    }
    return '';
  }

  /* Hz → MHz text for the form: 14074000 → "14.074", 145837500 → "145.8375". */
  function mhzText(hz) {
    return (hz / 1e6).toFixed(6).replace(/0{1,3}$/, '');
  }

  /* Radio mode → log mode. A data mode (DIGU…) keeps the digital mode chosen
   * in the form (FT8, FT4…): the radio cannot tell FT8 from PSK31.
   * `dataAsUsb`: the protocol reports a data mode as plain USB/LSB (Icom
   * USB-D, read with command 04) — then USB/LSB keep a digital mode too.
   * Kenwood/Thetis tell DIGU from USB: there, USB is always voice (SSB). */
  function logMode(radioMode, current, dataAsUsb) {
    var digital = DIGITAL.indexOf(current) >= 0;
    switch (radioMode) {
      case 'LSB': case 'USB': return dataAsUsb && digital ? current : 'SSB';
      case 'CW': case 'CW-R': return 'CW';
      case 'FM': case 'WFM': return 'FM';
      case 'AM': return 'AM';
      case 'RTTY': case 'RTTY-R': return 'RTTY';
      case 'DATA': return digital ? current : 'DIGI';
      case 'DV': return 'DIGI';
      default: return '';
    }
  }

  // Icom CI-V: FE FE <to> <from> <cmd> [data…] FD.
  var CIV_CTRL = 0xE0;                       // the computer's address
  var READ_ICOM = [0x03, 0x04];              // read frequency, read mode
  var ICOM_MODELS = {
    0xA2: 'IC-9700', 0x94: 'IC-7300', 0xA4: 'IC-705', 0x98: 'IC-7610',
    0x88: 'IC-7100', 0xAC: 'IC-905', 0x8C: 'IC-9100'
  };
  var ICOM_MODES = {
    0x00: 'LSB', 0x01: 'USB', 0x02: 'AM', 0x03: 'CW', 0x04: 'RTTY', 0x05: 'FM',
    0x06: 'WFM', 0x07: 'CW-R', 0x08: 'RTTY-R', 0x17: 'DV', 0x22: 'DV'
  };

  /* 5 BCD bytes, least significant first → Hz. null if not BCD. */
  function bcdToHz(bytes) {
    var digits = '';
    for (var i = 4; i >= 0; i--) {
      var hi = bytes[i] >> 4, lo = bytes[i] & 0x0F;
      if (hi > 9 || lo > 9) return null;
      digits += hi + '' + lo;
    }
    return parseInt(digits, 10);
  }

  /* Cut the complete frames out of a byte stream: {frames, rest}. */
  function civFrames(buf) {
    var frames = [], start = -1, i;
    for (i = 0; i < buf.length; i++) {
      if (buf[i] === 0xFE && buf[i + 1] === 0xFE && start < 0) { start = i; i++; continue; }
      if (buf[i] === 0xFD && start >= 0) { frames.push(buf.slice(start, i + 1)); start = -1; }
    }
    var rest = start >= 0 ? buf.slice(start) : new Uint8Array(0);
    if (rest.length > 64) rest = new Uint8Array(0);          // garbage: start over
    return { frames: frames, rest: rest };
  }

  /* One frame → {from, hz} or {from, mode} (null: echo, OK/NG, other). */
  function civParse(f) {
    if (f.length < 6) return null;
    var to = f[2], from = f[3], cmd = f[4], data = f.slice(5, f.length - 1);
    if (to !== CIV_CTRL && to !== 0x00) return null;         // our own echo / another controller
    if ((cmd === 0x03 || cmd === 0x00) && data.length >= 5) {
      var hz = bcdToHz(data);
      return hz ? { from: from, hz: hz } : null;
    }
    if ((cmd === 0x04 || cmd === 0x01) && data.length >= 1) {
      var mode = ICOM_MODES[data[0]];
      return mode ? { from: from, mode: mode } : null;
    }
    return null;
  }

  function civCommand(addr, cmd) {
    if (READ_ICOM.indexOf(cmd) < 0) throw new Error('CAT: write command refused');
    return new Uint8Array([0xFE, 0xFE, addr, CIV_CTRL, cmd, 0xFD]);
  }

  // Kenwood / TS-2000: "FA00014074000;" "MD2;".
  var READ_KENWOOD = ['FA;', 'MD;', 'ID;'];
  var KENWOOD_MODES = { '1': 'LSB', '2': 'USB', '3': 'CW', '4': 'FM', '5': 'AM',
                        '6': 'DATA', '7': 'CW-R', '9': 'DATA' };

  /* Text received → {items: [{hz}|{mode}|{id}], rest}. */
  function kwParse(text) {
    var parts = text.split(';'), rest = parts.pop(), items = [];
    parts.forEach(function (p) {
      p = p.trim();
      var m;
      if ((m = /^FA(\d{11})$/.exec(p))) items.push({ hz: parseInt(m[1], 10) });
      else if ((m = /^MD(\d)$/.exec(p)) && KENWOOD_MODES[m[1]]) items.push({ mode: KENWOOD_MODES[m[1]] });
      else if ((m = /^ID(\d{3})$/.exec(p))) items.push({ id: m[1] });
    });
    return { items: items, rest: rest.length > 64 ? '' : rest };
  }

  function kwCommand(cmd) {
    if (READ_KENWOOD.indexOf(cmd) < 0) throw new Error('CAT: write command refused');
    return new TextEncoder().encode(cmd);
  }

  window.ActCat = {
    bandOf: bandOf, mhzText: mhzText, logMode: logMode, bcdToHz: bcdToHz,
    civFrames: civFrames, civParse: civParse, civCommand: civCommand,
    kwParse: kwParse, kwCommand: kwCommand,
    READ_ICOM: READ_ICOM, READ_KENWOOD: READ_KENWOOD, ICOM_MODELS: ICOM_MODELS
  };

  // ── Page ───────────────────────────────────────────────────────────────

  var box = document.getElementById('act-cat');
  var form = document.querySelector('.act-log-form');
  if (!box || !form) return;

  function t(s, p) {
    var m = (window.ACT_I18N || {})[s] || s;
    return p ? m.replace(/\{(\w+)\}/g, function (_, k) { return p[k]; }) : m;
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }

  if (!window.isSecureContext) {
    box.innerHTML = '<p class="act-muted act-cat-note">' + esc(t('CAT : la page doit être ouverte en HTTPS.')) + '</p>';
    return;
  }
  if (!('serial' in navigator)) {
    box.innerHTML = '<p class="act-muted act-cat-note">' + esc(t('CAT : ouvrir cette page dans Chrome ou Edge, sur un ordinateur.')) + '</p>';
    return;
  }

  var PREF = 'tm.cat';
  var prefs = { proto: 'icom', baud: 115200, addr: 'auto', auto: false, usb: null };
  try { Object.assign(prefs, JSON.parse(localStorage.getItem(PREF) || '{}')); } catch (e) {}
  function savePrefs() { try { localStorage.setItem(PREF, JSON.stringify(prefs)); } catch (e) {} }

  var BAUDS = [4800, 9600, 19200, 38400, 57600, 115200];
  var ADDRS = ['auto', 0xA2, 0x94, 0xA4, 0x98, 0x88, 0xAC, 0x8C];

  box.innerHTML =
    '<div class="act-cat-head"><b>' + esc(t('Poste (CAT)')) + '</b> <span class="act-cat-ro">' + esc(t('lecture seule')) + '</span></div>' +
    '<div class="act-cat-row"><button type="button" class="act-cat-btn"></button>' +
    '<span class="act-cat-status" aria-live="polite"></span></div>' +
    '<details class="act-cat-cfg"><summary>' + esc(t('Réglages CAT')) + '</summary>' +
    '<label>' + esc(t('Protocole')) + ' <select data-k="proto">' +
    '<option value="icom">' + esc(t('Icom CI-V (IC-9700, IC-7300, IC-705…)')) + '</option>' +
    '<option value="kenwood">' + esc(t('Kenwood / TS-2000 (Thetis, SparkSDR…)')) + '</option></select></label>' +
    '<label>' + esc(t('Vitesse (bauds)')) + ' <select data-k="baud">' +
    BAUDS.map(function (b) { return '<option value="' + b + '">' + b + '</option>'; }).join('') + '</select></label>' +
    '<label class="act-cat-addr">' + esc(t('Adresse CI-V')) + ' <select data-k="addr">' +
    ADDRS.map(function (a) {
      return a === 'auto' ? '<option value="auto">' + esc(t('automatique')) + '</option>'
        : '<option value="' + a + '">' + a.toString(16).toUpperCase() + 'h — ' + ICOM_MODELS[a] + '</option>';
    }).join('') + '</select></label></details>';

  var btn = box.querySelector('.act-cat-btn');
  var status = box.querySelector('.act-cat-status');
  var cfg = box.querySelector('.act-cat-cfg');
  var selects = box.querySelectorAll('select[data-k]');
  selects.forEach(function (s) {
    s.value = String(prefs[s.dataset.k]);
    s.addEventListener('change', function () {
      var v = s.value;
      prefs[s.dataset.k] = s.dataset.k === 'proto' || v === 'auto' ? v : parseInt(v, 10);
      if (s.dataset.k === 'proto') {         // usual speed of each family
        prefs.baud = v === 'icom' ? 115200 : 9600;
        box.querySelector('select[data-k=baud]').value = String(prefs.baud);
      }
      syncCfg();
      savePrefs();
    });
  });
  function syncCfg() { box.querySelector('.act-cat-addr').hidden = prefs.proto !== 'icom'; }
  syncCfg();

  var freqInput = form.querySelector('input[name=freq]');
  var bandSel = form.querySelector('select[name=band]');
  var modeSel = form.querySelector('select[name=mode]');

  var port = null, reader = null, writer = null, timer = null, closing = false;
  var addr = null, model = '', lastRx = 0, last = { hz: 0, mode: '' };

  function setStatus(text, cls) {
    status.textContent = text;
    status.className = 'act-cat-status' + (cls ? ' ' + cls : '');
  }
  function render() {
    btn.textContent = port ? t('Déconnecter') : t('Connecter le poste');
    cfg.hidden = !!port;
    box.classList.toggle('is-on', !!port);
  }
  render();

  function hasOption(sel, v) { return Array.prototype.some.call(sel.options, function (o) { return o.value === v; }); }
  function pick(sel, v) {
    if (!v || sel.value === v || !hasOption(sel, v)) return;
    sel.value = v;
    sel.dispatchEvent(new Event('change', { bubbles: true }));   // spots, slot lock, dupes…
  }

  /* Push the radio's state into the form (the form is reset after each QSO). */
  function apply() {
    if (last.hz) {
      var txt = mhzText(last.hz);
      if (freqInput && freqInput.value !== txt) freqInput.value = txt;
    }
    setStatus([model, last.hz ? mhzText(last.hz) + ' MHz' : '', last.mode].filter(Boolean).join(' · '), 'is-ok');
  }
  function update(u) {
    lastRx = Date.now();
    if (u.hz && u.hz !== last.hz) {
      last.hz = u.hz;
      pick(bandSel, bandOf(u.hz));
    }
    if (u.mode && u.mode !== last.mode) {
      last.mode = u.mode;
      pick(modeSel, logMode(u.mode, modeSel.value, prefs.proto === 'icom'));
    }
    apply();
  }
  // The log form is reset after each QSO: log.html fires this event so that
  // the frequency comes back at once, without waiting for the next poll.
  window.addEventListener('act-cat-refresh', function () { if (port) apply(); });

  async function send(bytes) {
    if (writer) await writer.write(bytes);
  }

  async function poll() {
    try {
      if (prefs.proto === 'icom') {
        // Radio not identified yet: ask every known address at once; the
        // one that answers (or broadcasts a VFO change) is kept.
        var targets = addr ? [addr] : prefs.addr !== 'auto' ? [prefs.addr] : Object.keys(ICOM_MODELS).map(Number);
        for (var i = 0; i < targets.length; i++) {
          await send(civCommand(targets[i], 0x03));
          await send(civCommand(targets[i], 0x04));
        }
      } else {
        await send(kwCommand('FA;'));
        await send(kwCommand('MD;'));
      }
    } catch (e) { /* port gone: handled by the read loop */ }
    if (Date.now() - lastRx > 5000) {
      setStatus(t('Pas de réponse du poste : vérifiez la vitesse (bauds) et le réglage CI-V ou CAT.'), 'is-warn');
    }
  }

  async function readLoop() {
    var bin = new Uint8Array(0), text = '';
    var decoder = new TextDecoder();
    while (port && port.readable && !closing) {
      reader = port.readable.getReader();
      try {
        for (;;) {
          var r = await reader.read();
          if (r.done) break;
          if (prefs.proto === 'icom') {
            var joined = new Uint8Array(bin.length + r.value.length);
            joined.set(bin); joined.set(r.value, bin.length);
            var cut = civFrames(joined);
            bin = cut.rest;
            cut.frames.forEach(function (f) {
              var u = civParse(f);
              if (!u) return;
              if (!addr && ICOM_MODELS[u.from] !== undefined && (prefs.addr === 'auto' || prefs.addr === u.from)) {
                addr = u.from;
                model = ICOM_MODELS[u.from];
              }
              if (u.from === addr) update(u);
            });
          } else {
            var kw = kwParse(text + decoder.decode(r.value, { stream: true }));
            text = kw.rest;
            kw.items.forEach(function (u) {
              if (u.id) { model = u.id === '019' ? 'TS-2000' : 'Kenwood ' + u.id; return; }
              if (!model) model = 'Kenwood';
              update(u);
            });
          }
        }
      } catch (e) {
        break;                                                   // unplugged
      } finally {
        try { reader.releaseLock(); } catch (e) {}
      }
    }
    if (!closing) { await disconnect(); setStatus(t('Poste déconnecté.'), 'is-warn'); }
  }

  async function connect(p) {
    try {
      await p.open({ baudRate: prefs.baud, dataBits: 8, stopBits: 1, parity: 'none', flowControl: 'none' });
    } catch (e) {
      setStatus(t('Port occupé ou inaccessible : un autre logiciel l’utilise peut-être (WSJT-X…).'), 'is-warn');
      return;
    }
    port = p; closing = false; addr = null; model = ''; lastRx = Date.now();
    last = { hz: 0, mode: '' };
    writer = port.writable.getWriter();
    var info = port.getInfo ? port.getInfo() : {};
    prefs.auto = true;
    prefs.usb = info.usbVendorId ? [info.usbVendorId, info.usbProductId] : null;
    savePrefs();
    render();
    setStatus(t('Recherche du poste…'));
    if (prefs.proto === 'kenwood') { try { await send(kwCommand('ID;')); } catch (e) {} }
    readLoop();
    poll();
    timer = setInterval(poll, 1000);
  }

  async function disconnect() {
    closing = true;
    clearInterval(timer); timer = null;
    try { if (reader) await reader.cancel(); } catch (e) {}
    try { if (writer) { writer.releaseLock(); } } catch (e) {}
    try { if (port) await port.close(); } catch (e) {}
    port = reader = writer = null;
    render();
  }

  btn.addEventListener('click', async function () {
    if (port) {
      prefs.auto = false; savePrefs();
      await disconnect();
      setStatus('');
      return;
    }
    var p;
    try { p = await navigator.serial.requestPort(); } catch (e) { return; }   // dialog closed
    connect(p);
  });

  navigator.serial.addEventListener('disconnect', function (ev) {
    if (port && ev.target === port) { disconnect(); setStatus(t('Poste déconnecté.'), 'is-warn'); }
  });

  // Port already allowed on a previous visit: reconnect without the dialog.
  if (prefs.auto) {
    navigator.serial.getPorts().then(function (ports) {
      var p = ports.filter(function (x) {
        var i = x.getInfo ? x.getInfo() : {};
        return !prefs.usb || (i.usbVendorId === prefs.usb[0] && i.usbProductId === prefs.usb[1]);
      })[0];
      if (p) connect(p);
    });
  }
})();
