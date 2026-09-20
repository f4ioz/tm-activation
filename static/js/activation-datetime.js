/* Calendrier de saisie des dates et heures (flatpickr).
 *
 * - Souris/clavier : calendrier déroulant, heures au pas de 15 min, date
 *   affichée en toutes lettres ; la valeur envoyée reste celle d'un champ
 *   natif (« AAAA-MM-JJTHH:MM » ou « AAAA-MM-JJ »), le serveur ne change pas.
 * - Écran tactile : on garde le sélecteur du téléphone, plus pratique au doigt.
 * - Un champ « fin » suit son champ « début » (data-after) : déplacer le début
 *   déplace la fin d'autant (la durée est conservée), la fin ne peut pas être
 *   avant le début, et vaut début + 2 h tant qu'elle est vide.
 */
(function () {
  if (typeof flatpickr === 'undefined') return;
  // Pointeur fin = souris ou stylet : sinon, sélecteur natif du mobile.
  var fine = window.matchMedia && window.matchMedia('(pointer: fine)').matches;
  if (!fine) return;

  var lang = (document.documentElement.getAttribute('lang') || 'fr').slice(0, 2);
  if (flatpickr.l10ns && flatpickr.l10ns[lang]) flatpickr.localize(flatpickr.l10ns[lang]);

  var DEFAULT_LENGTH_MS = 2 * 3600 * 1000;
  var pickers = {};
  var previous = {};       // dernière valeur connue d'un début, pour le décalage

  function build(el) {
    var withTime = el.getAttribute('data-picker') === 'datetime';
    return flatpickr(el, {
      enableTime: withTime,
      time_24hr: true,
      minuteIncrement: 15,
      allowInput: true,
      altInput: true,
      altFormat: withTime ? 'D j M Y — H:i' : 'D j M Y',
      dateFormat: withTime ? 'Y-m-d\\TH:i' : 'Y-m-d',
      onChange: function (dates) { follow(el, dates[0]); }
    });
  }

  function follow(el, date) {
    var id = el.getAttribute('data-picker-id');
    if (!id || !date) return;
    var shift = previous[id] ? date.getTime() - previous[id] : 0;
    previous[id] = date.getTime();
    Object.keys(pickers).forEach(function (key) {
      var other = pickers[key];
      if (other.input.getAttribute('data-after') !== id) return;
      var end = other.selectedDates[0];
      if (!end) {
        other.setDate(new Date(date.getTime() + DEFAULT_LENGTH_MS), true);
      } else if (shift) {
        other.setDate(new Date(end.getTime() + shift), true);   // même durée
      } else if (end <= date) {
        other.setDate(new Date(date.getTime() + DEFAULT_LENGTH_MS), true);
      }
      other.set('minDate', date);
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('input[data-picker]'), function (el, i) {
    var id = el.getAttribute('data-picker-id') || 'p' + i;
    el.setAttribute('data-picker-id', id);
    pickers[id] = build(el);
  });

  // Un champ « fin » déjà en place doit respecter le début affiché.
  Object.keys(pickers).forEach(function (key) {
    var start = pickers[key].selectedDates[0];
    if (start) follow(pickers[key].input, start);
  });
})();
