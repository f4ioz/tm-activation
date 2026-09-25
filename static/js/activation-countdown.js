/* Countdown timers for activations.
 * Each .act-countdown element carries data-start / data-end in ISO UTC
 * ('YYYY-MM-DDTHH:MM'). Shows the time left before the start, or the
 * on-air / finished state.
 */
(function () {
  var els = Array.prototype.slice.call(document.querySelectorAll('.act-countdown'));
  if (!els.length) return;

  // Translated strings: window.ACT_I18N (filled by _base.html for non-French).
  function t(s, p) {
    var m = (window.ACT_I18N || {})[s] || s;
    return p ? m.replace(/\{(\w+)\}/g, function (_, k) { return p[k]; }) : m;
  }

  function toUTC(iso) {
    // 'YYYY-MM-DDTHH:MM' interpreted as UTC.
    return iso ? new Date(iso.slice(0, 16) + ':00Z').getTime() : NaN;
  }

  function fmt(ms) {
    var s = Math.floor(ms / 1000);
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60);
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    if (d > 0) return t('{d}j', { d: d }) + ' ' + pad(h) + 'h ' + pad(m) + 'm';
    if (h > 0) return pad(h) + 'h ' + pad(m) + 'm';
    if (m > 0) return m + 'm';
    return '< 1 min';
  }

  function tick() {
    var now = Date.now();
    els.forEach(function (el) {
      var start = toUTC(el.getAttribute('data-start'));
      var end = toUTC(el.getAttribute('data-end'));
      if (isNaN(start)) { el.textContent = ''; return; }
      if (now < start) {
        el.className = 'act-countdown is-soon';
        el.textContent = t('⏳ Débute dans {t}', { t: fmt(start - now) });
      } else if (now < end) {
        el.className = 'act-countdown is-onair';
        el.textContent = t('🔴 En direct — fin dans {t}', { t: fmt(end - now) });
      } else {
        el.className = 'act-countdown is-done';
        el.textContent = t('✓ Terminé');
      }
    });
  }

  tick();
  setInterval(tick, 1000);
})();
