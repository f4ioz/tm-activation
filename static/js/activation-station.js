/* Fiche du correspondant pendant la saisie du log : photo QRZ, boussole et
 * distance. Tout se recalcule côté navigateur à partir des deux locators (le
 * nôtre, dans data-home, et celui du correspondant) : pas d'aller-retour
 * serveur quand on tape un locator à la main.
 *
 * window.actStation(info) — info : {call, grid, image, name}
 * window.actStationGrid(grid) — juste le locator (champ « Locator » du log)
 * Tailles réglées dans les Réglages : data-photo et data-compass (0 = masqué).
 */
(function () {
  var box = document.getElementById('act-dir');
  if (!box) return;
  var photo = document.getElementById('act-dir-photo');
  var dial = document.getElementById('act-dir-compass');
  var meta = document.getElementById('act-dir-meta');
  var home = (box.dataset.home || '').toUpperCase();
  var photoPx = parseInt(box.dataset.photo || '0', 10) || 0;
  var compassPx = parseInt(box.dataset.compass || '0', 10) || 0;
  var T = window.ACT_I18N || {};

  function tr(key, fallback) { return T[key] || fallback; }

  /* Locator → [latitude, longitude] du CENTRE du carré (4, 6 ou 8 caractères). */
  function gridCenter(grid) {
    var g = (grid || '').trim().toUpperCase();
    if (!/^[A-R]{2}[0-9]{2}([A-X]{2}([0-9]{2})?)?$/.test(g)) return null;
    var lon = (g.charCodeAt(0) - 65) * 20 - 180;
    var lat = (g.charCodeAt(1) - 65) * 10 - 90;
    lon += parseInt(g[2], 10) * 2;
    lat += parseInt(g[3], 10);
    var dLon = 2, dLat = 1;
    if (g.length >= 6) {
      lon += (g.charCodeAt(4) - 65) * (2 / 24);
      lat += (g.charCodeAt(5) - 65) * (1 / 24);
      dLon = 2 / 24; dLat = 1 / 24;
    }
    if (g.length === 8) {
      lon += parseInt(g[6], 10) * (dLon / 10);
      lat += parseInt(g[7], 10) * (dLat / 10);
      dLon /= 10; dLat /= 10;
    }
    return [lat + dLat / 2, lon + dLon / 2];
  }

  function toRad(d) { return d * Math.PI / 180; }

  function distanceKm(a, b) {
    var R = 6371.0;
    var dLat = toRad(b[0] - a[0]), dLon = toRad(b[1] - a[1]);
    var s = Math.sin(dLat / 2) * Math.sin(dLat / 2)
      + Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
  }

  function bearingDeg(a, b) {
    var lat1 = toRad(a[0]), lat2 = toRad(b[0]), dLon = toRad(b[1] - a[1]);
    var y = Math.sin(dLon) * Math.cos(lat2);
    var x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }

  /* Rose des vents : cadran, graduations, lettres et aiguille. */
  function compass(deg, size) {
    var r = size / 2 - 2;
    var c = size / 2;
    var ticks = '';
    for (var a = 0; a < 360; a += 30) {
      var long = a % 90 === 0;
      var rad = toRad(a - 90);
      var r1 = r - (long ? 8 : 4);
      ticks += '<line x1="' + (c + Math.cos(rad) * r1).toFixed(1) + '" y1="'
        + (c + Math.sin(rad) * r1).toFixed(1) + '" x2="' + (c + Math.cos(rad) * r).toFixed(1)
        + '" y2="' + (c + Math.sin(rad) * r).toFixed(1) + '" class="act-dir-tick'
        + (long ? ' is-main' : '') + '"/>';
    }
    var letters = '';
    ['N', 'E', 'S', 'W'].forEach(function (letter, i) {
      var rad = toRad(i * 90 - 90);
      letters += '<text x="' + (c + Math.cos(rad) * (r - 17)).toFixed(1) + '" y="'
        + (c + Math.sin(rad) * (r - 17) + 4).toFixed(1) + '" class="act-dir-card">'
        + letter + '</text>';
    });
    var tip = toRad(deg - 90), tail = toRad(deg + 90);
    var needle = '<line x1="' + (c + Math.cos(tail) * (r - 26)).toFixed(1) + '" y1="'
      + (c + Math.sin(tail) * (r - 26)).toFixed(1) + '" x2="'
      + (c + Math.cos(tip) * (r - 12)).toFixed(1) + '" y2="'
      + (c + Math.sin(tip) * (r - 12)).toFixed(1) + '" class="act-dir-needle"/>'
      + '<circle cx="' + (c + Math.cos(tip) * (r - 12)).toFixed(1) + '" cy="'
      + (c + Math.sin(tip) * (r - 12)).toFixed(1) + '" r="3.5" class="act-dir-head"/>';
    return '<svg viewBox="0 0 ' + size + ' ' + size + '" width="' + size + '" height="' + size
      + '" role="img" aria-label="' + tr('Direction du correspondant', 'Direction du correspondant')
      + ' ' + Math.round(deg) + '°"><circle cx="' + c + '" cy="' + c + '" r="' + r
      + '" class="act-dir-dial"/>' + ticks + letters + needle
      + '<circle cx="' + c + '" cy="' + c + '" r="2.5" class="act-dir-pivot"/></svg>';
  }

  var state = { grid: '', image: '' };

  function render() {
    var target = gridCenter(state.grid);
    var here = gridCenter(home);
    var hasDirection = !!(target && here && compassPx);
    if (dial) {
      dial.innerHTML = hasDirection ? compass(bearingDeg(here, target), compassPx) : '';
      dial.hidden = !hasDirection;
    }
    if (photo) {
      var show = !!(state.image && photoPx);
      photo.hidden = !show;
      if (show && photo.getAttribute('src') !== state.image) photo.src = state.image;
      if (!show) photo.removeAttribute('src');
    }
    if (meta) {
      var bits = [];
      if (target && here) {
        var km = distanceKm(here, target);
        bits.push(Math.round(bearingDeg(here, target)) + '°');
        bits.push(km < 10 ? km.toFixed(1) + ' km' : Math.round(km).toLocaleString() + ' km');
        if (state.grid) bits.push(state.grid);
      }
      meta.textContent = bits.join(' · ');
    }
    box.hidden = !(hasDirection || (state.image && photoPx));
  }

  window.actStation = function (info) {
    state.grid = ((info && info.grid) || '').toUpperCase();
    state.image = (info && info.image) || '';
    render();
  };
  window.actStationGrid = function (grid) {
    var g = (grid || '').toUpperCase();
    if (g === state.grid) return;
    state.grid = g;
    render();
  };
  window.actStationClear = function () { state.grid = ''; state.image = ''; render(); };
  render();
})();
