/* Public contacts map (board /<callsign>), Leaflet.
 * One point per locator × band × mode: the COLOUR shows the mode, the SHAPE
 * shows the band (configurable in Réglages → Carte des contacts). Points in
 * the same square are spread in a ring so they all stay clickable;
 * callsigns are shown as labels from LABEL_ZOOM upwards.
 * Data: <script type="application/json" id="act-map-data">. */
(function () {
  var el = document.getElementById('act-map');
  var dataEl = document.getElementById('act-map-data');
  if (!el || !dataEl || typeof L === 'undefined') return;
  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }

  var LABEL_ZOOM = 6;
  var MAX_LABEL_CALLS = 3;
  var SPREAD_PX = 9;            // spacing of points in the same square
  var style = data.style || {};
  var styled = !!style.enabled;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Without custom styling, all points keep the site's cyan.
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

  /* Symbol outline in a 100×100 box (the SVG is scaled afterwards). */
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

  // Standard OpenStreetMap base layer.
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map);

  var points = data.points || [];

  // Offset of points in the same square: the first stays at the centre, the
  // next ones circle around at SPREAD_PX (in pixels, so constant when zooming).
  var perGrid = {};
  points.forEach(function (p) { (perGrid[p.grid] = perGrid[p.grid] || []).push(p); });

  var bounds = [];
  var markers = [];             // { marker, band, mode } for the filters
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
      markers.push({ marker: m, band: p.band, mode: p.mode });
      bounds.push([p.lat, p.lon]);
    });
  });

  // Activation station (locator from config): always-labelled marker.
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

  /* Legend: only the bands and modes actually worked. With the "filters"
   * option, each entry becomes a checkbox — a point stays visible as long
   * as ITS band AND ITS mode are checked. */
  var NEUTRAL = '#5b6b7c';
  var legend = document.getElementById('act-map-legend');
  var modes = data.modes || [];
  var bands = data.bands || [];
  var filtering = !!style.filters;

  function symbol(kind, value) {
    return kind === 'mode' ? shapeSvg('circle', colorOf(value), 14)
      : shapeSvg(shapeOf(value), styled ? NEUTRAL : PLAIN, 14);
  }

  function entry(kind, value) {
    var sym = symbol(kind, value) + esc(value);
    if (!filtering) return '<span class="act-legend-item">' + sym + '</span>';
    return '<label class="act-legend-item is-filter"><input type="checkbox" checked'
      + ' data-kind="' + kind + '" data-value="' + esc(value) + '">' + sym + '</label>';
  }

  if (legend && points.length && (styled || filtering)) {
    var html = modes.map(function (m) { return entry('mode', m); }).join('')
      + bands.map(function (b) { return entry('band', b); }).join('');
    if (filtering) {
      html += '<span class="act-legend-actions">'
        + '<button type="button" data-all>' + esc(legend.dataset.all || 'Tout') + '</button>'
        + '<button type="button" data-none>' + esc(legend.dataset.none || 'Aucun') + '</button>'
        + '</span>';
    }
    legend.innerHTML = html;
  }

  if (legend && filtering) {
    var boxes = legend.querySelectorAll('input[type=checkbox]');

    function apply() {
      var on = { mode: {}, band: {} };
      Array.prototype.forEach.call(boxes, function (box) {
        on[box.dataset.kind][box.dataset.value] = box.checked;
      });
      markers.forEach(function (m) {
        // Band or mode missing from the legend (empty field): always visible.
        var show = (!m.mode || on.mode[m.mode] !== false)
          && (!m.band || on.band[m.band] !== false);
        if (show && !map.hasLayer(m.marker)) map.addLayer(m.marker);
        else if (!show && map.hasLayer(m.marker)) map.removeLayer(m.marker);
      });
    }

    legend.addEventListener('change', apply);
    legend.addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-all], button[data-none]');
      if (!btn) return;
      var value = btn.hasAttribute('data-all');
      Array.prototype.forEach.call(boxes, function (box) { box.checked = value; });
      apply();
    });
  }
})();
