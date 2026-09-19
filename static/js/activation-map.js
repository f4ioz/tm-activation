/* Carte publique des contacts (board /<indicatif>), Leaflet.
 * Un point par locator (centre du carré) ; les indicatifs s'affichent en
 * étiquette à partir de LABEL_ZOOM (illisibles à l'échelle du monde).
 * Données : <script type="application/json" id="act-map-data">. */
(function () {
  var el = document.getElementById('act-map');
  var dataEl = document.getElementById('act-map-data');
  if (!el || !dataEl || typeof L === 'undefined') return;
  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }

  var LABEL_ZOOM = 6;
  var MAX_LABEL_CALLS = 3;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var map = L.map(el, { worldCopyJump: true, minZoom: 2, maxZoom: 19 }).setView([48.8, 2.5], 4);

  // Fond OpenStreetMap standard.
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map);

  var bounds = [];
  (data.points || []).forEach(function (p) {
    var m = L.circleMarker([p.lat, p.lon], {
      className: 'act-map-pt', radius: 4 + Math.min(p.calls.length, 6), weight: 1.5, fillOpacity: 0.55
    }).addTo(map);
    var label = p.calls.slice(0, MAX_LABEL_CALLS).join(', ');
    if (p.calls.length > MAX_LABEL_CALLS) label += ' +' + (p.calls.length - MAX_LABEL_CALLS);
    m.bindTooltip(esc(label), { permanent: true, direction: 'right', offset: [6, 0], className: 'act-map-label' });
    m.bindPopup('<b>' + esc(p.grid) + '</b><br>' + p.calls.map(esc).join('<br>'));
    bounds.push([p.lat, p.lon]);
  });

  // Station d'activation (locator de config) : repère toujours étiqueté.
  var home = data.home || {};
  if (home.pos) {
    L.circleMarker(home.pos, { className: 'act-map-home', radius: 7, weight: 2, fillOpacity: 0.8 })
      .addTo(map)
      .bindTooltip(esc(home.call), { permanent: true, direction: 'left', offset: [-8, 0], className: 'act-map-label is-home' });
    bounds.push(home.pos);
  }

  if (bounds.length > 1) map.fitBounds(bounds, { padding: [24, 24], maxZoom: LABEL_ZOOM });
  else if (bounds.length === 1) map.setView(bounds[0], 5);

  function syncLabels() { el.classList.toggle('act-map-labels', map.getZoom() >= LABEL_ZOOM); }
  map.on('zoomend', syncLabels);
  syncLabels();
})();
