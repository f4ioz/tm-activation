/* Date and time input: a quality calendar when Internet is available,
 * a simple calendar otherwise, the phone's own picker on touch screens.
 *
 * 1. Air Datepicker, loaded from the Internet (airy calendar, hour and
 *    minute sliders). If it does not answer within 2 s — club network
 *    without Internet — we do not wait.
 * 2. Fallback: flatpickr, bundled with the app (so always available in
 *    the standalone version).
 * 3. Last resort: the browser's native field, unchanged.
 *
 * The submitted value is always "YYYY-MM-DDTHH:MM" (or "YYYY-MM-DD"):
 * the server sees no difference.
 *
 * Shortcuts (Maintenant, Ce soir…, +2 h): buttons rendered by the templates,
 * wired here, working whichever calendar is active.
 */
(function () {
  var CDN_JS = 'https://unpkg.com/air-datepicker@3.5.3/air-datepicker.js';
  var CDN_JS_SRI = 'sha384-K1aTGb0e2SnCOyrnr+BX23z7N8DHKN5oLkdG4v1cRFitBfc/qBzF6SYIO0yDmGwn';
  var CDN_CSS = 'https://unpkg.com/air-datepicker@3.5.3/air-datepicker.css';
  var CDN_CSS_SRI = 'sha384-+SLUj9NZCsFzQ4x212CLVVLWdvqjbf2bjQnoORFq4gN/+cRaEQ6kJmLC6aWPsx94';
  var CDN_TIMEOUT = 2000;
  var DEFAULT_LENGTH_MIN = 120;

  var fields = Array.prototype.slice.call(document.querySelectorAll('input[data-picker]'));
  if (!fields.length) return;

  var lang = (document.documentElement.getAttribute('lang') || 'fr').slice(0, 2);
  var LOCALES = {
    fr: {
      days: ['dimanche', 'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi'],
      daysShort: ['dim', 'lun', 'mar', 'mer', 'jeu', 'ven', 'sam'],
      daysMin: ['di', 'lu', 'ma', 'me', 'je', 've', 'sa'],
      months: ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
               'septembre', 'octobre', 'novembre', 'décembre'],
      monthsShort: ['janv', 'févr', 'mars', 'avr', 'mai', 'juin', 'juil', 'août', 'sept', 'oct', 'nov', 'déc'],
      today: "Aujourd'hui", clear: 'Effacer', dateFormat: 'dd/MM/yyyy', timeFormat: 'HH:mm', firstDay: 1
    },
    en: {
      days: ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'],
      daysShort: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
      daysMin: ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'],
      months: ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
               'September', 'October', 'November', 'December'],
      monthsShort: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
      today: 'Today', clear: 'Clear', dateFormat: 'dd MMM yyyy', timeFormat: 'HH:mm', firstDay: 1
    }
  };
  var locale = LOCALES[lang] || LOCALES.fr;

  // ── Values: always in a native field's format ─────────────────────────────
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function parse(value) {
    if (!value) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/.exec(value);
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0));
  }

  function format(date, withTime) {
    var d = date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
    return withTime ? d + 'T' + pad(date.getHours()) + ':' + pad(date.getMinutes()) : d;
  }

  // Changes made by the script must not trigger each other: setting a
  // minimum date sometimes deselects the chosen date.
  var busy = false;
  function quietly(fn) {
    busy = true;
    try { fn(); } finally { busy = false; }
  }

  // ── Three ways to edit a field, same interface ───────────────────────────
  function nativeField(el, withTime) {
    return {
      el: el, withTime: withTime,
      get: function () { return parse(el.value); },
      set: function (date) { el.value = format(date, withTime); },
      min: function () {}
    };
  }

  function airField(el, withTime, onChange) {
    var visible = document.createElement('input');
    visible.type = 'text';
    visible.className = el.className;
    visible.setAttribute('autocomplete', 'off');
    if (el.hasAttribute('aria-describedby')) visible.setAttribute('aria-describedby', el.getAttribute('aria-describedby'));
    el.parentNode.insertBefore(visible, el);
    el.type = 'hidden';
    var picker = new AirDatepicker(visible, {
      locale: locale,
      timepicker: withTime,
      minutesStep: 15,
      autoClose: !withTime,
      buttons: withTime ? ['today', 'clear'] : ['today', 'clear'],
      dateFormat: 'E dd MMM yyyy',      // the time is appended by the timepicker
      altField: el,
      // A function, because Air Datepicker formats have no escaped literal:
      // we write "YYYY-MM-DDTHH:MM" ourselves.
      altFieldDateFormat: function (date) { return format(date, withTime); },
      selectedDates: parse(el.value) ? [parse(el.value)] : [],
      onSelect: function (data) { if (!busy && data.date instanceof Date) onChange(data.date); }
    });
    var api = {
      el: el, withTime: withTime,
      get: function () { return picker.selectedDates[0] || parse(el.value); },
      // updateTime: without it, the calendar would keep the previous time.
      set: function (date) {
        quietly(function () { picker.selectDate(date, { updateTime: true }); });
        el.value = format(date, withTime);
      },
      min: function (date) {
        var keep = api.get();
        quietly(function () { picker.update({ minDate: date }); });
        if (keep) api.set(keep);          // the update may deselect
      }
    };
    return api;
  }

  function flatField(el, withTime, onChange) {
    var picker = flatpickr(el, {
      enableTime: withTime,
      time_24hr: true,
      minuteIncrement: 15,
      allowInput: true,
      altInput: true,
      altFormat: withTime ? 'D j M Y — H:i' : 'D j M Y',
      dateFormat: withTime ? 'Y-m-d\\TH:i' : 'Y-m-d',
      locale: lang === 'fr' && flatpickr.l10ns && flatpickr.l10ns.fr ? flatpickr.l10ns.fr : undefined,
      onChange: function (dates) { if (!busy && dates[0]) onChange(dates[0]); }
    });
    var api = {
      el: el, withTime: withTime,
      get: function () { return picker.selectedDates[0] || parse(el.value); },
      set: function (date) {
        quietly(function () { picker.setDate(date, false); });
        el.value = format(date, withTime);
      },
      min: function (date) {
        var keep = api.get();
        quietly(function () { picker.set('minDate', date); });
        if (keep) api.set(keep);
      }
    };
    return api;
  }

  // ── Field links: the end follows the start, duration preserved ──────────
  var byId = {};
  var previous = {};

  function changed(field, date) {
    var id = field.el.getAttribute('data-picker-id');
    if (!id || !date) return;
    var shift = previous[id] ? date.getTime() - previous[id] : 0;
    previous[id] = date.getTime();
    fields.forEach(function (other) {
      if (other.getAttribute('data-after') !== id) return;
      var target = byId[other.getAttribute('data-picker-id')];
      var end = target.get();
      if (!end) target.set(new Date(date.getTime() + DEFAULT_LENGTH_MIN * 60000));
      else if (shift) target.set(new Date(end.getTime() + shift));
      else if (end <= date) target.set(new Date(date.getTime() + DEFAULT_LENGTH_MIN * 60000));
      target.min(date);
    });
  }

  function attach(make) {
    fields.forEach(function (el, i) {
      var id = el.getAttribute('data-picker-id') || 'p' + i;
      el.setAttribute('data-picker-id', id);
      var withTime = el.getAttribute('data-picker') === 'datetime';
      var field = make(el, withTime, function (date) { changed(field, date); });
      byId[id] = field;
      var now = field.get();
      if (now) previous[id] = now.getTime();
    });
    wireShortcuts();
  }

  // ── Shortcuts (buttons rendered by the templates) ────────────────────────
  function nowIn(zone) {
    var d = new Date();
    if (zone !== 'utc') return d;
    return new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours(), d.getUTCMinutes());
  }

  function wireShortcuts() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-quick-for]'), function (box) {
      var target = byId[box.getAttribute('data-quick-for')];
      if (!target) return;
      var zone = target.el.getAttribute('data-tz') || 'local';
      Array.prototype.forEach.call(box.querySelectorAll('button'), function (btn) {
        btn.addEventListener('click', function () {
          var set = btn.getAttribute('data-set');
          var add = btn.getAttribute('data-add');
          var date;
          if (add) {                                   // + N minutes after the start
            var startId = target.el.getAttribute('data-after');
            var start = startId && byId[startId] ? byId[startId].get() : null;
            if (!start) return;
            date = new Date(start.getTime() + parseInt(add, 10) * 60000);
          } else if (set === 'now') {
            date = nowIn(zone);
          } else {                                     // "today 20:00" / "tomorrow 09:00"
            var parts = (set || '').split(' ');
            var hm = (parts[1] || '00:00').split(':');
            date = nowIn(zone);
            if (parts[0] === 'tomorrow') date.setDate(date.getDate() + 1);
            date.setHours(+hm[0], +hm[1], 0, 0);
          }
          target.set(date);
          changed(target, date);
        });
      });
    });
  }

  // ── Calendar choice ──────────────────────────────────────────────────────
  // Touch screen: the phone's picker remains the most convenient; the
  // shortcuts are still wired.
  if (!(window.matchMedia && window.matchMedia('(pointer: fine)').matches)) {
    attach(nativeField);
    return;
  }

  function withFallback() {
    attach(typeof flatpickr !== 'undefined' ? flatField : nativeField);
  }

  function loadCdn(done) {
    if (navigator.onLine === false) return done(false);
    var css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = CDN_CSS;
    css.integrity = CDN_CSS_SRI;
    css.crossOrigin = 'anonymous';
    document.head.appendChild(css);
    var js = document.createElement('script');
    js.src = CDN_JS;
    js.integrity = CDN_JS_SRI;
    js.crossOrigin = 'anonymous';
    var settled = false;
    function finish(ok) {
      if (settled) return;
      settled = true;
      if (!ok && css.parentNode) css.parentNode.removeChild(css);
      done(ok);
    }
    js.onload = function () { finish(typeof AirDatepicker !== 'undefined'); };
    js.onerror = function () { finish(false); };
    setTimeout(function () { finish(typeof AirDatepicker !== 'undefined'); }, CDN_TIMEOUT);
    document.head.appendChild(js);
  }

  loadCdn(function (ok) {
    if (ok) attach(airField);
    else withFallback();
  });
})();
