/* Red Nun — universal sortable tables */
(function () {
  'use strict';
  function parseValue(text) {
    text = (text || '').trim();
    if (text === '' || text === '-' || text === '\u2014') return { t: 'empty', v: '' };
    var m = text.match(/^-?\$?([\d,]+(?:\.\d+)?)$/);
    if (m) { var n = parseFloat(text.replace(/[$,]/g, '')); if (!isNaN(n)) return { t: 'num', v: n }; }
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return { t: 'num', v: new Date(text).getTime() };
    m = text.match(/^(\d+)d$/);
    if (m) return { t: 'num', v: parseInt(m[1], 10) };
    m = text.match(/^-?([\d,]+(?:\.\d+)?)%$/);
    if (m) { var p = parseFloat(text.replace(/[,%]/g, '')); if (!isNaN(p)) return { t: 'num', v: p }; }
    return { t: 'text', v: text.toLowerCase() };
  }
  function sortTable(table, colIdx, dir) {
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var ca = a.cells[colIdx], cb = b.cells[colIdx];
      if (!ca || !cb) return 0;
      var va = parseValue(ca.textContent), vb = parseValue(cb.textContent);
      if (va.t === 'empty' && vb.t === 'empty') return 0;
      if (va.t === 'empty') return 1;
      if (vb.t === 'empty') return -1;
      var cmp = (typeof va.v === 'number' && typeof vb.v === 'number')
        ? va.v - vb.v
        : String(va.v).localeCompare(String(vb.v));
      return dir === 'asc' ? cmp : -cmp;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }
  function updateArrows(th, dir) {
    var table = th.closest('table');
    if (!table) return;
    var allTh = table.querySelectorAll('th');
    Array.prototype.forEach.call(allTh, function (t) {
      var a = t.querySelector('.rn-sort-arrow');
      if (a) a.remove();
    });
    var arrow = document.createElement('span');
    arrow.className = 'rn-sort-arrow';
    arrow.textContent = dir === 'asc' ? ' \u25B2' : ' \u25BC';
    arrow.style.fontSize = '0.7em';
    arrow.style.opacity = '0.6';
    arrow.style.marginLeft = '4px';
    th.appendChild(arrow);
  }
  document.addEventListener('click', function (e) {
    var th = e.target && e.target.closest && e.target.closest('th');
    if (!th) return;
    var table = th.closest('table');
    if (!table) return;
    if (!th.closest('.rn-main')) return;
    if (!table.tBodies[0] || !table.tBodies[0].rows.length) return;
    if (th.onclick || th.getAttribute('onclick') ||
        (th.parentElement && th.parentElement.getAttribute('onclick'))) return;
    var row = th.parentElement;
    if (!row) return;
    var colIdx = -1;
    for (var i = 0; i < row.cells.length; i++) {
      if (row.cells[i] === th) { colIdx = i; break; }
    }
    if (colIdx < 0) return;
    var curCol = parseInt(table.dataset.rnSortCol || '-1', 10);
    var curDir = table.dataset.rnSortDir || '';
    var next;
    if (curCol === colIdx) {
      next = curDir === 'asc' ? 'desc' : 'asc';
    } else {
      next = 'asc';
    }
    table.dataset.rnSortCol = String(colIdx);
    table.dataset.rnSortDir = next;
    sortTable(table, colIdx, next);
    updateArrows(th, next);
  });
  function markClickable(root) {
    var ths = (root || document).querySelectorAll('.rn-main table th');
    Array.prototype.forEach.call(ths, function (t) {
      if (t.dataset.rnSortMarked) return;
      t.dataset.rnSortMarked = '1';
      t.style.cursor = 'pointer';
      t.style.userSelect = 'none';
    });
  }
  function onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }
  onReady(function () {
    markClickable();
    if (window.MutationObserver && document.body) {
      var obs = new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          var m = muts[i];
          if (!m.addedNodes) continue;
          for (var j = 0; j < m.addedNodes.length; j++) {
            var n = m.addedNodes[j];
            if (n.nodeType === 1) {
              if (n.tagName === 'TABLE' || (n.querySelector && n.querySelector('table'))) {
                markClickable(n);
              }
            }
          }
        }
      });
      obs.observe(document.body, { childList: true, subtree: true });
    }
  });
})();
