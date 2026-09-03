/* lib.js — the pure parts of the Klikk Journals add-in.
 *
 * app.js is one ~3,000-line IIFE that closes over the DOM, Office.js and the
 * network, so nothing inside it can be required from node and nothing inside
 * it can be tested without a workbook. The functions here take values and
 * return values: no DOM, no Office, no fetch, no closure state. That is the
 * whole selection rule. They are loaded as a classic script before app.js
 * (window.KlikkLib) and also exported for node, so `npm test` can pin them
 * without a build step. Two files, no bundler.
 *
 * Behaviour is IDENTICAL to what these functions did inside app.js — they
 * were moved, not rewritten. Each one is here because it was, or could
 * plausibly be, the site of a bug you only see by looking at a rendered
 * sheet.
 */
(function (root, factory) {
  var api = factory();
  if (root) root.KlikkLib = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  /* Consecutive cube rows that share formatting, as runs.
   *
   * Formatting a cube row by row is one Excel range call per row, which is the
   * difference between a sheet that renders and one that appears to hang, so
   * rows are shaded and indented in RUNS of consecutive rows that share a
   * depth.
   *
   * The key is the REAL depth. Clamping it (it was Math.min(depth, 2)) made a
   * depth-3 row look identical to a depth-2 one, so the first supplier under
   * an account joined the ACCOUNT's run and was indented with the account's
   * depth -- into column C, leaving its own column D flush left. The second
   * supplier onwards began a fresh run and indented correctly, which is why
   * only the FIRST CHILD of each parent looked wrong. Callers still clamp when
   * they pick a fill colour or an indent level; only the run boundaries here
   * use the unclamped depth.
   */
  function depthRuns(rows) {
    var run = null;
    var runs = [];
    (rows || []).forEach(function (r, i) {
      var kind = (r.is_total ? 'T' : 'L') + r.depth;
      if (run && run.kind === kind && run.to === i - 1) {
        run.to = i;
      } else {
        run = { kind: kind, from: i, to: i, isTotal: r.is_total, depth: r.depth };
        runs.push(run);
      }
    });
    return runs;
  }

  /* {dimf: '{"fin_year":["FY2026"]}'} — omitted entirely when nothing is
     narrowed, so an unfiltered cube's URL and comment anchors stay as they
     were before filters existed. */
  function dimfParam(spec) {
    var f = spec && spec.filters ? spec.filters : {};
    var live = {};
    Object.keys(f).forEach(function (k) {
      // NOT sorted. The subset's order is the layout order -- the server lays
      // rows and columns out in exactly this sequence -- so sorting here would
      // silently throw away the arrangement the user made.
      if (f[k] && f[k].length) live[k] = f[k].slice();
    });
    return Object.keys(live).length ? { dimf: JSON.stringify(live) } : {};
  }

  /* rtotals / ctotals — which fields carry a total.

     Each is sent ONLY when it differs from the server's default, so an
     unchanged cube produces the same request it always did. rtotals is
     omitted while every row level still shows its subtotal; ctotals is
     omitted while no column field asks for one. */
  function totalsParams(spec) {
    var t = (spec && spec.totals) || {};
    var rows = spec.rows || [], cols = spec.cols || [];
    function on(k, zone) {
      return Object.prototype.hasOwnProperty.call(t, k) ? !!t[k] : zone === 'rows';
    }
    var out = {};
    var parents = rows.slice(0, -1);
    if (parents.some(function (k) { return !on(k, 'rows'); })) {
      var keep = parents.filter(function (k) { return on(k, 'rows'); });
      // An empty value is meaningful here: "no row subtotals at all".
      out.rtotals = keep.length ? keep.join(',') : '__none__';
    }
    var ct = cols.slice(0, -1).filter(function (k) { return on(k, 'cols'); });
    if (ct.length) out.ctotals = ct.join(',');
    return out;
  }

  /* Do two column paths agree on every level ABOVE the given one?
     That is what makes them part of the same parent's span. */
  function samePrefix(a, b, level) {
    for (var i = 0; i <= level; i++) {
      if ((a[i] === undefined ? '' : a[i]) !== (b[i] === undefined ? '' : b[i])) return false;
    }
    return true;
  }

  /* An ISO date as an Excel serial, so the cell sorts and filters as a date
     instead of as text. Anything that is not yyyy-mm-dd is passed through
     untouched rather than turned into a wrong number. */
  function toSerial(iso) {
    if (!iso) return '';
    var p = iso.split('-');
    if (p.length !== 3) return iso;
    return Date.UTC(+p[0], +p[1] - 1, +p[2]) / 86400000 + 25569;
  }

  /* Which sheet a Build writes to.
   *
   * Build is in place when a cube sheet of OURS is in front -- the way a
   * PivotTable refreshes. Every build used to add a sheet unconditionally,
   * which produced Cube 2 ... Cube 5 in eleven seconds of dragging and meant
   * a layout could never be edited, only re-created. Returning null means
   * "add a new sheet"; returning an id means "rewrite that sheet in place".
   */
  function cubeTarget(activeSheet) {
    var b = activeSheet && activeSheet.binding;
    return (b && b.kind === 'cube' && b.spec && activeSheet.id) ? activeSheet.id : null;
  }

  return {
    depthRuns: depthRuns,
    dimfParam: dimfParam,
    totalsParams: totalsParams,
    samePrefix: samePrefix,
    toSerial: toSerial,
    cubeTarget: cubeTarget
  };
});
