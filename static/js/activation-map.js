/* Carte publique des contacts (board /<indicatif>), Leaflet.
 * Un point par locator × bande × mode : la COULEUR dit le mode, la FORME dit
 * la bande (réglables dans Réglages → Carte des contacts). Les points d'un
 * même carré sont écartés en couronne pour rester tous cliquables ; les
 * indicatifs s'affichent en étiquette à partir de LABEL_ZOOM.
 * Données : <script type="application/json" id="act-map-data">. */
(function () {
  var el = document.getElementById('act-map');
  var dataEl = document.getElementById('act-map-data');
  if (!el || !dataEl || typeof L === 'undefined') return;
  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }

  var LABEL_ZOOM = 6;
  var MAX_LABEL_CALLS = 3;
  var SPREAD_PX = 9;            // écart des points d'un même carré
  var style = data.style || {};
  var styled = !!style.enabled;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Sans style personnalisé, tous les points gardent le cyan du site.
  var PLAIN = (getComputedStyle(document.documentElement).getPropertyValue('--cyan') || '').trim()
    || '#3d7ea6';

  function colorOf(mode) {
    if (!styled) return PLAIN;
    return (style.mode_colors || {})[mode] || style.mode_default;
  }

  function shapeOf(band) {
    if (!styled) return 'circle';
    return (style.band_shapes || {})[band] || style.band_default;
  }

  /* Contour du symbole dans une case de 100×100 (SVG mis à l'échelle ensuite). */
  function shapeSvg(shape, color, size) {
    var fill = 'fill="' + color + '" fill-opacity="0.75" stroke="#1b2430" stroke-width="8"';
    var body;
    switch (shape) {
      case 'square': body = '<rect x="18" y="18" width="64" height="64" ' + fill + '/>'; break;
      case 'triangle': body = '<polygon points="50,12 88,82 12,82" ' + fill + '/>'; break;
      case 'diamond': body = '<polygon points="50,8 92,50 50,92 8,50" ' + fill + '/>'; break;
      case 'star':
        body = '<polygon points="50,6 62,38 96,38 68,58 79,92 50,71 21,92 32,58 4,38 38,38" '
          + fill + '/>';
        break;
      case 'hexagon': body = '<polygon points="50,8 88,29 88,71 50,92 12,71 12,29" ' + fill + '/>'; break;
      case 'cross':
        body = '<polygon points="34,8 66,8 66,34 92,34 92,66 66,66 66,92 34,92 34,66 8,66 8,34 34,34" '
          + fill + '/>';
        break;
      case 'pentagon': body = '<polygon points="50,7 93,38 77,90 23,90 7,38" ' + fill + '/>'; break;
      default: body = '<circle cx="50" cy="50" r="38" ' + fill + '/>';
    }
    return '<svg viewBox="0 0 100 100" width="' + size + '" height="' + size + '">' + body + '</svg>';
  }

  var map = L.map(el, { worldCopyJump: true, minZoom: 2, maxZoom: 19 }).setView([48.8, 2.5], 4);

  // Fond OpenStreetMap standard.
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map);

  var points = data.points || [];

  // Décalage des points d'un même carré : le premier reste au centre, les
  // suivants tournent autour à SPREAD_PX (en pixels, donc constant au zoom).
  var perGrid = {};
  points.forEach(function (p) { (perGrid[p.grid] = perGrid[p.grid] || []).push(p); });

  var bounds = [];
  Object.keys(perGrid).forEach(function (grid) {
    var group = perGrid[grid];
    group.forEach(function (p, i) {
      var dx = 0, dy = 0;
      if (group.length > 1) {
        var angle = (2 * Math.PI * i) / group.length;
        var ring = SPREAD_PX * (1 + Math.floor(i / 8) * 0.9);
        dx = Math.round(Math.cos(angle) * ring);
        dy = Math.round(Math.sin(angle) * ring);
      }
      var size = 13 + Math.min(p.calls.length - 1, 5) * 2;
      var icon = L.divIcon({
        className: 'act-map-pt',
        html: shapeSvg(shapeOf(p.band), colorOf(p.mode), size),
        iconSize: [size, size],
        iconAnchor: [size / 2 - dx, size / 2 - dy]
      });
      var m = L.marker([p.lat, p.lon], { icon: icon, keyboard: false }).addTo(map);
      var label = p.calls.slice(0, MAX_LABEL_CALLS).join(', ');
      if (p.calls.length > MAX_LABEL_CALLS) label += ' +' + (p.calls.length - MAX_LABEL_CALLS);
      m.bindTooltip(esc(label), {
        permanent: true, direction: 'right', offset: [6 + dx, -dy], className: 'act-map-label'
      });
      var head = esc(p.grid) + ' · ' + esc(p.band) + ' · ' + esc(p.mode);
      m.bindPopup('<b>' + head + '</b><br>' + p.calls.map(esc).join('<br>'));
      bounds.push([p.lat, p.lon]);
    });
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

  // Légende : seulement les bandes et les modes réellement travaillés.
  var legend = document.getElementById('act-map-legend');
  if (legend && styled && points.length) {
    var neutral = '#5b6b7c';
    var html = '';
    (data.modes || []).forEach(function (mode) {
      html += '<span class="act-legend-item">' + shapeSvg('circle', colorOf(mode), 14)
        + esc(mode) + '</span>';
    });
    (data.bands || []).forEach(function (band) {
      html += '<span class="act-legend-item">' + shapeSvg(shapeOf(band), neutral, 14)
        + esc(band) + '</span>';
    });
    legend.innerHTML = html;
  }
})();
