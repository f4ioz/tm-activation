/* QSO audio on the log page (optional, internal listening).
 *
 * Recorder: the radio's audio output — USB sound card of an IC-9700 /
 * IC-7300 / IC-705, virtual audio cable of Thetis… — is recorded by the
 * operator's browser in short Opus segments sent to the server as they come.
 * At each cut the next recorder starts before the previous one stops: nothing
 * is lost between two segments.
 *
 * Player: a ▶ button on each QSO of the log (operators) and, when public
 * listening is on, on the hunter's own QSOs of the public page (rows carrying
 * data-audio). The segments around the QSO are decoded and laid out on one
 * time line (gaps stay silent), drawn as a waveform with a marker at the
 * instant the QSO was logged; a click seeks.
 */
(function () {
  'use strict';
  var box = document.getElementById('act-audio');      // recorder: log page only

  var SEGMENT_MS = window.ACT_AUDIO_SEGMENT_MS || 20000;   // ~60 kB at 24 kbit/s
  var BITRATE = 24000;
  var QUEUE_MAX = 60;                                       // segments kept while offline

  function t(s, p) {
    var m = (window.ACT_I18N || {})[s] || s;
    return p ? m.replace(/\{(\w+)\}/g, function (_, k) { return p[k]; }) : m;
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function hms(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return pad(Math.floor(s / 3600)) + ':' + pad(Math.floor(s % 3600 / 60)) + ':' + pad(s % 60);
  }
  function utc(ms) { var d = new Date(ms); return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()); }

  // ── Recorder ───────────────────────────────────────────────────────────

  var canRecord = window.isSecureContext && navigator.mediaDevices && window.MediaRecorder;
  if (!box) {
    /* public page: player only */
  } else if (!window.isSecureContext) {
    box.innerHTML = '<p class="act-muted act-cat-note">' + esc(t('Audio : la page doit être ouverte en HTTPS.')) + '</p>';
  } else if (!canRecord) {
    box.innerHTML = '<p class="act-muted act-cat-note">' + esc(t('Audio : ce navigateur ne sait pas enregistrer.')) + '</p>';
  } else {
    initRecorder();
  }

  function initRecorder() {
    var PREF = 'tm.audio';
    var prefs = { device: '', auto: false };
    try { Object.assign(prefs, JSON.parse(localStorage.getItem(PREF) || '{}')); } catch (e) {}
    function savePrefs() { try { localStorage.setItem(PREF, JSON.stringify(prefs)); } catch (e) {} }

    box.innerHTML =
      '<div class="act-cat-head"><b>' + esc(t('Audio')) + '</b> <span class="act-cat-ro">' + esc(t('écoute interne')) + '</span></div>' +
      '<div class="act-cat-row"><button type="button" class="act-cat-btn"></button>' +
      '<span class="act-audio-level" aria-hidden="true"><i></i></span>' +
      '<span class="act-cat-status" aria-live="polite"></span></div>' +
      '<details class="act-cat-cfg"><summary>' + esc(t('Source audio')) + '</summary>' +
      '<label>' + esc(t('Entrée')) + ' <select data-k="device"><option value="">' + esc(t('par défaut')) + '</option></select></label>' +
      '<p class="act-muted act-cat-note">' + esc(t('Choisir la carte son du poste (USB Audio CODEC pour les Icom) ou le câble audio virtuel du logiciel SDR.')) + '</p></details>';

    var btn = box.querySelector('.act-cat-btn');
    var status = box.querySelector('.act-cat-status');
    var level = box.querySelector('.act-audio-level i');
    var select = box.querySelector('select[data-k=device]');

    var stream = null, actx = null, analyser = null, rec = null, cutTimer = null, tick = null;
    var mime = '', recording = false, since = 0, sent = 0, queue = [], flushing = false, lastError = '';

    function mimeType() {
      return ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm'].filter(function (m) {
        return MediaRecorder.isTypeSupported(m);
      })[0] || '';
    }

    function render() {
      btn.textContent = recording ? t('Arrêter') : t('Enregistrer l’audio');
      box.classList.toggle('is-on', recording);
      if (!recording) { level.style.width = '0'; return; }
      var parts = ['● ' + hms(Date.now() - since), t('{n} envoyés', { n: sent })];
      if (queue.length) parts.push(t('{n} en attente', { n: queue.length }));
      status.textContent = parts.join(' · ');
      status.className = 'act-cat-status ' + (queue.length > 2 || lastError ? 'is-warn' : 'is-rec');
      if (lastError) status.title = lastError;
    }

    async function openStream() {
      var c = { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 };
      if (prefs.device) c.deviceId = { exact: prefs.device };
      try {
        return await navigator.mediaDevices.getUserMedia({ audio: c });
      } catch (e) {
        if (!prefs.device) throw e;
        delete c.deviceId;                              // input gone: default one
        return navigator.mediaDevices.getUserMedia({ audio: c });
      }
    }

    /* Inputs are named only once the permission is granted. The radio's
     * sound card is chosen by itself the first time. */
    async function listDevices() {
      var inputs = (await navigator.mediaDevices.enumerateDevices()).filter(function (d) { return d.kind === 'audioinput'; });
      select.innerHTML = '<option value="">' + esc(t('par défaut')) + '</option>' + inputs.map(function (d) {
        return '<option value="' + esc(d.deviceId) + '">' + esc(d.label || d.deviceId.slice(0, 8)) + '</option>';
      }).join('');
      if (!prefs.device) {
        var radio = inputs.filter(function (d) { return /usb audio codec|codec|usb audio/i.test(d.label); })[0];
        if (radio) { prefs.device = radio.deviceId; savePrefs(); return true; }
      }
      select.value = prefs.device;
      return false;
    }

    function newRecorder() {
      var r = new MediaRecorder(stream, { mimeType: mime, audioBitsPerSecond: BITRATE });
      var chunks = [];
      r.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      r.onstop = function () {
        var dur = (r.stopAt || Date.now()) - r.t0;
        // A crumb left when stopping (< 0.3 s) is not worth a request.
        if (chunks.length && dur >= 300) upload(new Blob(chunks, { type: mime.split(';')[0] }), r.t0, dur);
      };
      r.t0 = Date.now();
      r.start();
      return r;
    }

    function cut() {
      var old = rec;
      rec = newRecorder();                              // the next one first: no gap
      old.stopAt = Date.now();
      old.stop();
    }

    function upload(blob, t0, dur) {
      queue.push({ blob: blob, t0: t0, dur: dur });
      if (queue.length > QUEUE_MAX) queue.shift();
      flush();
    }

    async function flush(keepalive) {
      if (flushing) return;
      flushing = true;
      while (queue.length) {
        var s = queue[0];
        try {
          var r = await fetch('/activation/audio?start=' + s.t0 + '&dur=' + Math.round(s.dur) + '&now=' + Date.now(), {
            method: 'POST', body: s.blob, headers: { 'Content-Type': s.blob.type },
            credentials: 'same-origin', keepalive: !!keepalive && s.blob.size < 60000
          });
          if (r.status === 400 || r.status === 413) {        // refused for good: dropped
            queue.shift();
            try { lastError = (await r.json()).error || ''; } catch (e) { lastError = String(r.status); }
            continue;
          }
          if (!r.ok || r.redirected) throw new Error(String(r.status));   // session lost, offline…
          queue.shift(); sent++; lastError = '';
        } catch (e) {
          break;                                              // retried with the next segment
        }
      }
      flushing = false;
      render();
    }

    function meter() {
      if (!recording || !analyser) return;
      var data = new Float32Array(analyser.fftSize);
      analyser.getFloatTimeDomainData(data);
      var peak = 0;
      for (var i = 0; i < data.length; i++) peak = Math.max(peak, Math.abs(data[i]));
      level.style.width = Math.min(100, Math.round(peak * 100)) + '%';
      level.classList.toggle('is-clip', peak > 0.98);
      requestAnimationFrame(meter);
    }

    async function start() {
      mime = mimeType();
      try {
        stream = await openStream();
        if (await listDevices()) {                       // radio's card found: switch to it
          stream.getTracks().forEach(function (tr) { tr.stop(); });
          stream = await openStream();
        }
      } catch (e) {
        status.textContent = t('Micro refusé ou introuvable : autoriser l’accès à l’entrée audio.');
        status.className = 'act-cat-status is-warn';
        prefs.auto = false; savePrefs();
        return;
      }
      try {
        actx = new AudioContext();
        analyser = actx.createAnalyser();
        analyser.fftSize = 1024;
        actx.createMediaStreamSource(stream).connect(analyser);
      } catch (e) { analyser = null; }
      recording = true; since = Date.now(); sent = 0; lastError = '';
      rec = newRecorder();
      cutTimer = setInterval(cut, SEGMENT_MS);
      tick = setInterval(render, 1000);
      prefs.auto = true; savePrefs();
      render();
      meter();
    }

    function stop(keepAuto) {
      if (!recording) return;
      recording = false;
      clearInterval(cutTimer); clearInterval(tick);
      if (rec && rec.state !== 'inactive') { rec.stopAt = Date.now(); rec.stop(); }
      if (stream) stream.getTracks().forEach(function (tr) { tr.stop(); });
      if (actx) actx.close();
      stream = actx = analyser = rec = null;
      if (!keepAuto) { prefs.auto = false; savePrefs(); }
      status.textContent = '';
      render();
    }

    btn.addEventListener('click', function () { if (recording) stop(); else start(); });
    select.addEventListener('change', function () {
      prefs.device = select.value; savePrefs();
      if (recording) { stop(true); start(); }
    });
    // Leaving the page: the segment in progress is sent (keepalive).
    window.addEventListener('pagehide', function () {
      if (!recording || !rec) return;
      rec.onstop = (function (orig) {
        return function () { orig.call(rec); flush(true); };
      })(rec.onstop);
      rec.stopAt = Date.now();
      rec.stop();
    });
    render();

    // Recording on at the previous visit, permission already granted: resume.
    if (prefs.auto && navigator.permissions) {
      navigator.permissions.query({ name: 'microphone' }).then(function (p) {
        if (p.state === 'granted') start();
      }).catch(function () {});
    }
  }

  // ── Player ─────────────────────────────────────────────────────────────

  /* Excerpt URL of a row: given by the page (public), or the operators'
   * route for the log's rows. */
  function clipUrl(tr) {
    if (tr.dataset.audio) return tr.dataset.audio;
    return box && tr.dataset.qso ? '/activation/contacts/' + encodeURIComponent(tr.dataset.qso) + '/audio' : '';
  }

  function enhance() {
    document.querySelectorAll('tr[data-audio], tr[data-qso]').forEach(function (tr) {
      var td = tr.querySelector('.act-actions'), url = clipUrl(tr);
      if (!td || !url || td.querySelector('.act-play')) return;
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'act-play';
      b.title = t('Écouter le QSO');
      b.textContent = '▶';
      b.dataset.url = url;
      td.insertBefore(b, td.firstChild);
    });
  }
  enhance();
  document.body.addEventListener('htmx:afterSwap', enhance);

  var player = null, playCtx = null, clip = null, buffer = null, source = null;
  var pos = 0, startedAt = 0, raf = 0;

  function buildPlayer() {
    player = document.createElement('div');
    player.className = 'act-player';
    player.hidden = true;
    player.innerHTML =
      '<div class="act-player-head"><b class="act-player-title"></b>' +
      '<button type="button" class="act-player-close" title="' + esc(t('Fermer')) + '">✕</button></div>' +
      '<div class="act-player-body"><button type="button" class="act-player-toggle">▶</button>' +
      '<canvas class="act-player-wave" height="64"></canvas></div>' +
      '<div class="act-player-foot"><span class="act-player-time"></span><span class="act-player-msg"></span>' +
      '<button type="button" class="act-player-hide" hidden></button></div>';
    document.body.appendChild(player);
    player.querySelector('.act-player-close').addEventListener('click', closePlayer);
    player.querySelector('.act-player-hide').addEventListener('click', toggleHidden);
    player.querySelector('.act-player-toggle').addEventListener('click', function () { if (source) pause(); else play(); });
    player.querySelector('canvas').addEventListener('click', function (e) {
      if (!buffer) return;
      var r = e.currentTarget.getBoundingClientRect();
      var playing = !!source;
      if (playing) pause();
      pos = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * buffer.duration;
      draw();
      if (playing) play();
    });
    window.addEventListener('resize', function () { if (!player.hidden) draw(); });
  }

  function message(text) { player.querySelector('.act-player-msg').textContent = text || ''; }

  function closePlayer() {
    pause();
    player.hidden = true;
    clip = buffer = null;
  }

  function renderHide(data) {
    var b = player.querySelector('.act-player-hide');
    b.hidden = !(data && data.can_hide);
    if (data) b.textContent = data.hidden ? t('Montrer au public') : t('Masquer au public');
  }

  /* Admins: keep this QSO's recording off the public page (or put it back). */
  async function toggleHidden() {
    if (!clip) return;
    var body = new FormData();
    body.append('hidden', clip.hidden ? '0' : '1');
    try {
      var r = await fetch('/activation/contacts/' + clip.id + '/audio-public',
                          { method: 'POST', body: body, credentials: 'same-origin' });
      if (!r.ok) throw new Error(String(r.status));
      clip.hidden = (await r.json()).hidden;
      renderHide(clip);
      message(clip.hidden ? t('Masqué au public.') : '');
    } catch (e) { message(t('Audio indisponible.')); }
  }

  async function openClip(url) {
    if (!player) buildPlayer();
    pause();
    clip = buffer = null; pos = 0;
    renderHide(null);
    player.hidden = false;
    player.querySelector('.act-player-title').textContent = '…';
    player.querySelector('.act-player-time').textContent = '';
    message(t('Chargement de l’audio…'));
    draw();
    var data;
    try {
      var r = await fetch(url, { credentials: 'same-origin' });
      if (!r.ok || r.redirected) throw new Error(String(r.status));
      data = await r.json();
    } catch (e) {
      message(t('Audio indisponible.'));
      return;
    }
    var d = new Date(data.at_ms);
    player.querySelector('.act-player-title').textContent =
      data.call + ' · ' + data.band + ' ' + data.mode + ' · ' + data.operator + ' · ' +
      pad(d.getUTCDate()) + '/' + pad(d.getUTCMonth() + 1) + ' ' + utc(data.at_ms) + ' UTC';
    if (!data.segments.length) { message(t('Pas d’audio enregistré autour de ce QSO.')); return; }
    clip = data;
    renderHide(data);
    try {
      playCtx = playCtx || new AudioContext();
      var rate = playCtx.sampleRate;
      var total = Math.max(1, Math.ceil((data.to_ms - data.from_ms) / 1000 * rate));
      var buf = playCtx.createBuffer(1, total, rate);
      var out = buf.getChannelData(0);
      for (var i = 0; i < data.segments.length; i++) {
        var seg = data.segments[i];
        var bytes = await (await fetch(seg.url, { credentials: 'same-origin' })).arrayBuffer();
        var audio = await playCtx.decodeAudioData(bytes);
        var src = audio.getChannelData(0);
        var offset = Math.round((seg.start_ms - data.from_ms) / 1000 * rate);
        var from = Math.max(0, -offset), to = Math.min(src.length, total - offset);
        for (var k = from; k < to; k++) out[offset + k] = src[k];
      }
      buffer = buf;
      // Start where the recording starts, not in the silence before it.
      pos = Math.max(0, Math.min(buf.duration, (data.segments[0].start_ms - data.from_ms) / 1000));
      message('');
      draw();
    } catch (e) {
      message(t('Audio illisible par ce navigateur.'));
    }
  }

  function draw() {
    var canvas = player.querySelector('canvas');
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = Math.max(1, Math.round(w * dpr));
    canvas.height = Math.max(1, Math.round(h * dpr));
    var g = canvas.getContext('2d');
    g.scale(dpr, dpr);
    var css = getComputedStyle(player);
    g.clearRect(0, 0, w, h);
    if (!buffer) return;
    var data = buffer.getChannelData(0), step = data.length / w, mid = h / 2;
    g.fillStyle = css.getPropertyValue('--wave') || '#4d9eff';
    for (var x = 0; x < w; x++) {
      var a = Math.floor(x * step), b = Math.min(data.length, Math.floor((x + 1) * step)), peak = 0;
      for (var i = a; i < b; i++) { var v = data[i] < 0 ? -data[i] : data[i]; if (v > peak) peak = v; }
      var bar = Math.max(1, peak * (h - 4));
      g.fillRect(x, mid - bar / 2, 1, bar);
    }
    // The QSO: when it was logged.
    var qx = (clip.at_ms - clip.from_ms) / (clip.to_ms - clip.from_ms) * w;
    g.fillStyle = css.getPropertyValue('--marker') || '#ef4444';
    g.fillRect(Math.round(qx) - 1, 0, 2, h);
    // Play head.
    var px = pos / buffer.duration * w;
    g.fillStyle = css.getPropertyValue('--head') || '#e4e4e7';
    g.fillRect(Math.round(px), 0, 1, h);
    var at = clip.from_ms + pos * 1000;
    var rel = Math.round((at - clip.at_ms) / 1000);
    player.querySelector('.act-player-time').textContent =
      utc(at) + ' UTC · ' + t('QSO {t}', { t: (rel < 0 ? '−' : '+') + hms(Math.abs(rel) * 1000).slice(3) });
  }

  function play() {
    if (!buffer || source) return;
    if (pos >= buffer.duration - 0.05) pos = 0;
    if (playCtx.state === 'suspended') playCtx.resume();
    source = playCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(playCtx.destination);
    source.onended = function () {
      if (!source) return;
      pos = Math.min(buffer.duration, playCtx.currentTime - startedAt);
      source = null;
      player.querySelector('.act-player-toggle').textContent = '▶';
      draw();
    };
    source.start(0, pos);
    startedAt = playCtx.currentTime - pos;
    player.querySelector('.act-player-toggle').textContent = '❚❚';
    (function frame() {
      if (!source) return;
      pos = Math.min(buffer.duration, playCtx.currentTime - startedAt);
      draw();
      raf = requestAnimationFrame(frame);
    })();
  }

  function pause() {
    cancelAnimationFrame(raf);
    if (!source) return;
    var s = source;
    source = null;
    pos = Math.min(buffer ? buffer.duration : 0, playCtx.currentTime - startedAt);
    try { s.stop(); } catch (e) {}
    if (player) player.querySelector('.act-player-toggle').textContent = '▶';
    if (buffer) draw();
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.act-play');
    if (b) openClip(b.dataset.url);
  });
})();
