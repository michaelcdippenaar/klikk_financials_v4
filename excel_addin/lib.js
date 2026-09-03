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

  /* The SAME filters, written for the comment ANCHOR rather than for the query.
     Two forms on purpose, and they must not be merged back into one.

     dimfParam above is the QUERY. Its list order IS the layout order -- the
     server lays rows and columns out in exactly that sequence -- so a subset
     naming every member still carries the arrangement the user built by hand.
     Collapsing it there would silently throw that arrangement away.

     The ANCHOR is only ever asked "which cut of the ledger is this figure
     from?", and order cannot change that answer. A subset that names EVERY
     member of a dimension narrows nothing, so it is the same cut as no filter
     at all -- and the add-in already writes "no filter" as an omitted key
     (README: "An empty subset means ALL members, never none"). Writing the
     same cut two different ways is what buried MC's comments under a dimf
     enumerating twelve years and a hundred and forty-four months.

     `totals` is {dim: {members: [...], truncated: bool}} from
     journals/pivot/members/?dim=<key> -- note ?dim=, not ?dimension=.

     Collapsing is deliberately PROVE-IT-OR-KEEP-IT. A dimension is dropped
     only when we hold its full member list and the subset covers every value
     in it. No entry, a capped list, or an empty one proves nothing, so the
     verbose form survives -- a filter wrongly called "all" would re-point a
     comment at a figure it was never written about.

     THIS RULE IS PART OF COMMENT IDENTITY. cell_key is derived from the
     collapsed form, so changing what this collapses does not change how an
     anchor reads -- it changes which stored comment a cell IS. Nothing errors:
     the add-in writes a SECOND comment on a figure MC has already annotated,
     while the original sits under the old key. app.cube_comments was migrated
     on 2026-09-03 to match this function; it is the live rule and any other
     implementation follows it. See README, "The collapse rule is part of
     comment IDENTITY", before touching this. */
  function anchorDimfParam(spec, totals) {
    var f = spec && spec.filters ? spec.filters : {};
    var live = {};
    Object.keys(f).forEach(function (k) {
      var vals = f[k];
      if (!vals || !vals.length) return;          // already means ALL members
      var t = totals && totals[k];
      if (t && t.truncated !== true && t.members && t.members.length) {
        var picked = {};
        vals.forEach(function (v) { picked[String(v)] = true; });
        // Coverage, not a count: a subset can hold values that are no longer
        // in the member list because the journal filters moved under it, so
        // equal LENGTHS do not mean equal SETS.
        var coversAll = t.members.every(function (m) {
          return picked[String(m)] === true;
        });
        if (coversAll) return;                    // narrows nothing -> omit
      }
      live[k] = vals.slice();
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

  /* ── column widths across a rebuild ────────────────────── */

  /* What a cube column is worth when nobody has said otherwise.
     The row-label columns are not all the same job: the outer ones hold short
     codes (REVENUE, OVERHEADS), the innermost holds the long account name, and
     the value columns are uniform so the eye can compare down one. */
  var CUBE_W = { rowDim: 130, rowLeaf: 330, value: 104 };

  function defaultCubeWidth(i, nRowDims) {
    if (i < nRowDims - 1) return CUBE_W.rowDim;
    if (i === nRowDims - 1) return CUBE_W.rowLeaf;
    return CUBE_W.value;
  }

  /* The width every column of a rebuilt cube should end up at.
   *
   * A rebuild rewrites the sheet in place, and until now it re-applied the
   * defaults above afterwards -- so hand-sizing the columns (narrow ones with
   * the last one wide, which is how the row-label hierarchy reads) survived
   * only until the next Build. Manual sizing is intent, not noise: a column
   * that was already on this sheet keeps the width it has, and only a
   * genuinely NEW column takes a default.
   *
   * `prev` is null for a fresh sheet (everything defaults). Otherwise it is
   * `{ widths: [...], nRowDims: n }` read off the sheet before it was cleared,
   * `widths` positional and holding null where a width could not be read.
   *
   * Position alone is not identity, because the layout changes between
   * rebuilds. So columns are matched by ROLE:
   *
   *   - row-label columns keep their width only if there are still as many of
   *     them; if the layout went from two row dimensions to one, old column 0
   *     held a class code and new column 0 holds an account name, and carrying
   *     130 across would be stretching a saved width onto an unrelated column.
   *   - the k-th value column keeps the k-th old value column's width, so
   *     adding months appends columns rather than shifting every width along.
   *   - the grand total keeps the old grand total's width -- it is the last
   *     column in both layouts, at different indices.
   *   - anything with no counterpart (the new months) takes the default.
   */
  function cubeWidths(layout, prev) {
    var nRowDims = layout.nRowDims;
    var width = nRowDims + layout.nCols + 1;
    /* A sheet whose used range cannot hold even one value column and a total
       is not a cube we rendered -- someone edited it. Default the lot rather
       than guess. */
    var usable = !!(prev && prev.widths && prev.widths.length >= prev.nRowDims + 2);
    var out = [];
    for (var i = 0; i < width; i++) {
      var j = -1;
      if (!usable) j = -1;                                    // nothing to keep
      else if (i < nRowDims) j = (prev.nRowDims === nRowDims) ? i : -1;
      else if (i === width - 1) j = prev.widths.length - 1;
      else {
        j = prev.nRowDims + (i - nRowDims);
        if (j >= prev.widths.length - 1) j = -1;   // a column the old sheet did not have
      }
      var kept = (j >= 0 && j < prev.widths.length) ? prev.widths[j] : null;
      out.push(kept > 0 ? kept : defaultCubeWidth(i, nRowDims));
    }
    return out;
  }

  /* The same, for a detail sheet: fixed columns, so position IS identity.
     `defaults` is one width per column; `prev` as above, or null. */
  function detailWidths(defaults, prev) {
    var have = (prev && prev.widths) ? prev.widths : [];
    return defaults.map(function (d, i) {
      return have[i] > 0 ? have[i] : d;
    });
  }

  /* Widths as runs of adjacent columns that share one width.
   *
   * Setting columnWidth column by column is one queued range call per column,
   * and a wide cube is fifty of them -- the same shape of cost that made
   * per-row formatting appear to hang. Adjacent equal widths collapse into one
   * call, so the defaults on a fresh sheet cost exactly the three calls they
   * did when they were three literals. */
  function widthRuns(widths) {
    var runs = [];
    var start = 0;
    for (var i = 1; i <= widths.length; i++) {
      if (i === widths.length || widths[i] !== widths[start]) {
        runs.push({ col: start, span: i - start, width: widths[start] });
        start = i;
      }
    }
    return runs;
  }

  return {
    depthRuns: depthRuns,
    dimfParam: dimfParam,
    anchorDimfParam: anchorDimfParam,
    totalsParams: totalsParams,
    samePrefix: samePrefix,
    toSerial: toSerial,
    cubeTarget: cubeTarget,
    defaultCubeWidth: defaultCubeWidth,
    cubeWidths: cubeWidths,
    detailWidths: detailWidths,
    widthRuns: widthRuns
  };
});
