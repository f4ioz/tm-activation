/* Saisie des dates et heures : calendrier de qualité si Internet est là,
 * calendrier simple sinon, sélecteur du téléphone sur écran tactile.
 *
 * 1. Air Datepicker, chargé depuis Internet (calendrier aéré, heures et
 *    minutes au curseur). S'il ne répond pas en 2 s — réseau du club sans
 *    Internet — on n'attend pas.
 * 2. Repli : flatpickr, fourni avec l'application (donc toujours disponible
 *    dans la version autonome).
 * 3. Dernier repli : le champ natif du navigateur, inchangé.
 *
 * La valeur envoyée reste dans tous les cas « AAAA-MM-JJTHH:MM » (ou
 * « AAAA-MM-JJ ») : le serveur ne voit aucune différence.
 *
 * Raccourcis (Maintenant, Ce soir…, +2 h) : boutons rendus par les templates,
 * branchés ici, valables quel que soit le calendrier actif.
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

  // ── Valeurs : toujours le format d'un champ natif ────────────────────────
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

  // Les modifications faites par le script ne doivent pas se relancer entre
  // elles : poser une date minimale désélectionne parfois la date choisie.
  var busy = false;
  function quietly(fn) {
    busy = true;
    try { fn(); } finally { busy = false; }
  }

  // ── Trois façons d'éditer un champ, même interface ───────────────────────
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
      dateFormat: 'E dd MMM yyyy',      // l'heure est ajoutée par le timepicker
      altField: el,
      // Une fonction, car les formats d'Air Datepicker n'ont pas de littéral
      // échappé : c'est nous qui écrivons « AAAA-MM-JJTHH:MM ».
      altFieldDateFormat: function (date) { return format(date, withTime); },
      selectedDates: parse(el.value) ? [parse(el.value)] : [],
      onSelect: function (data) { if (!busy && data.date instanceof Date) onChange(data.date); }
    });
    var api = {
      el: el, withTime: withTime,
      get: function () { return picker.selectedDates[0] || parse(el.value); },
      // updateTime : sans lui, le calendrier garderait l'heure précédente.
      set: function (date) {
        quietly(function () { picker.selectDate(date, { updateTime: true }); });
        el.value = format(date, withTime);
      },
      min: function (date) {
        var keep = api.get();
        quietly(function () { picker.update({ minDate: date }); });
        if (keep) api.set(keep);          // la mise à jour peut désélectionner
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

  // ── Liens entre champs : la fin suit le début, durée conservée ───────────
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

  // ── Raccourcis (boutons rendus par les templates) ────────────────────────
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
          if (add) {                                   // + N minutes après le début
            var startId = target.el.getAttribute('data-after');
            var start = startId && byId[startId] ? byId[startId].get() : null;
            if (!start) return;
            date = new Date(start.getTime() + parseInt(add, 10) * 60000);
          } else if (set === 'now') {
            date = nowIn(zone);
          } else {                                     // « today 20:00 » / « tomorrow 09:00 »
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

  // ── Choix du calendrier ──────────────────────────────────────────────────
  // Écran tactile : le sélecteur du téléphone reste le plus pratique ; on
  // branche tout de même les raccourcis.
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
