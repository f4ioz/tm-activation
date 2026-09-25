/* Mouse row selection in the ADIF tables (export of a selection, import
 * preview): click = check/uncheck, Shift+click = range, click-and-drag =
 * "paint" several rows. Group filters (data-filter) hide AND uncheck rows
 * outside the group: only visible, checked rows are submitted. */
(function () {
  // Translated strings: window.ACT_I18N (filled by _base.html for non-French).
  function t(s, p) {
    var m = (window.ACT_I18N || {})[s] || s;
    return p ? m.replace(/\{(\w+)\}/g, function (_, k) { return p[k]; }) : m;
  }

  document.querySelectorAll('form[data-sel]').forEach(function (form) {
    var rows = Array.prototype.slice.call(form.querySelectorAll('tr[data-sel-row]'));
    var filters = Array.prototype.slice.call(form.querySelectorAll('[data-filter]'));
    var countEl = form.querySelector('[data-sel-count]');
    var toggleAll = form.querySelector('[data-sel-toggle]');
    var go = form.querySelector('button[type=submit]');

    function box(tr) { return tr.querySelector('input[type=checkbox]'); }
    function shown() { return rows.filter(function (tr) { return !tr.hidden; }); }

    function refresh() {
      var n = 0;
      rows.forEach(function (tr) {
        var on = box(tr).checked;
        tr.classList.toggle('is-sel', on);
        if (on) n++;
      });
      var vis = shown().length;
      if (countEl) countEl.textContent = t(n > 1 ? '{n} / {total} sélectionnés' : '{n} / {total} sélectionné', { n: n, total: vis });
      if (go) go.disabled = n === 0;
      if (toggleAll) {
        toggleAll.checked = vis > 0 && n === vis;
        toggleAll.indeterminate = n > 0 && n < vis;
      }
    }
    function setRows(list, on) {
      list.forEach(function (tr) { box(tr).checked = on; });
      refresh();
    }

    function applyFilters() {
      rows.forEach(function (tr) {
        var ok = filters.every(function (f) {
          return !f.value || tr.getAttribute('data-' + f.getAttribute('data-filter')) === f.value;
        });
        tr.hidden = !ok;
        if (!ok) box(tr).checked = false;
      });
      refresh();
    }
    filters.forEach(function (f) { f.addEventListener('change', applyFilters); });

    form.querySelectorAll('[data-sel-all]').forEach(function (b) {
      b.addEventListener('click', function () { setRows(shown(), true); });
    });
    form.querySelectorAll('[data-sel-none]').forEach(function (b) {
      b.addEventListener('click', function () { setRows(rows, false); });
    });
    if (toggleAll) toggleAll.addEventListener('change', function () { setRows(shown(), toggleAll.checked); });

    var last = -1, painting = false, paintOn = false;
    rows.forEach(function (tr, i) {
      var cb = box(tr);
      // Toggling happens on mousedown (which enables dragging): the checkbox's
      // native mouse click is cancelled so it does not toggle back. With the
      // keyboard (space, e.detail === 0) the checkbox behaves normally.
      cb.addEventListener('click', function (e) { if (e.detail !== 0) e.preventDefault(); });
      cb.addEventListener('change', refresh);
      tr.addEventListener('mousedown', function (e) {
        if (e.button !== 0 || e.target.closest('a, button, select')) return;
        e.preventDefault();                 // no text selection
        paintOn = !cb.checked;
        if (e.shiftKey && last >= 0) {
          var a = Math.min(last, i), b = Math.max(last, i);
          setRows(rows.slice(a, b + 1).filter(function (r) { return !r.hidden; }), paintOn);
        } else {
          cb.checked = paintOn;
          painting = true;
          refresh();
        }
        last = i;
      });
      tr.addEventListener('mouseenter', function () {
        if (painting && !tr.hidden) { cb.checked = paintOn; last = i; refresh(); }
      });
    });
    document.addEventListener('mouseup', function () { painting = false; });
    refresh();
  });
})();
