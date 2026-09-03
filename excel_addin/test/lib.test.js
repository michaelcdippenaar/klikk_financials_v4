/* Pure-logic tests. No DOM, no Excel, no network — these are the parts of the
   add-in that are just values in, values out, and the cheapest place to pin a
   formatting rule that is otherwise only visible by looking at a sheet. */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const ADDIN_DIR = process.env.KLIKK_ADDIN_DIR
  ? path.resolve(process.env.KLIKK_ADDIN_DIR)
  : path.resolve(__dirname, '..');
const lib = require(path.join(ADDIN_DIR, 'lib.js'));

/* ── run merging ─────────────────────────────────────────────────────────
   The 2026-09-03 bug: runs were keyed on Math.min(depth, 2), so a depth-3 row
   following a depth-2 row of the same kind merged into it and was indented at
   the parent's level. Only the FIRST child of each parent was wrong — the
   second began a fresh run — which is exactly why it survived a look at the
   sheet. */

const L = (depth) => ({ is_total: false, depth: depth });
const T = (depth) => ({ is_total: true, depth: depth });

test('a deeper row starts its own run, so the first child keeps its indent', () => {
  // ACCOUNT total at depth 2, then its suppliers at depth 3.
  const runs = lib.depthRuns([T(2), L(3), L(3), L(3)]);
  assert.deepStrictEqual(
    runs.map(r => [r.from, r.to, r.depth]),
    [[0, 0, 2], [1, 3, 3]],
    'the depth-3 rows must form one run of their own, starting at the FIRST child'
  );
});

test('every depth past the shading cap still splits its own run', () => {
  // Shading only has three fills, so depths 2, 3 and 4 look alike on screen.
  // They must still be three runs, or rows 1 and 2 get row 0's indent.
  const runs = lib.depthRuns([L(2), L(3), L(4)]);
  assert.strictEqual(runs.length, 3);
  assert.deepStrictEqual(runs.map(r => r.depth), [2, 3, 4]);
});

test('consecutive rows of the same depth and kind merge into one run', () => {
  const runs = lib.depthRuns([L(1), L(1), L(1)]);
  assert.deepStrictEqual(runs.map(r => [r.from, r.to]), [[0, 2]]);
});

test('a total and a leaf at the same depth are different runs', () => {
  const runs = lib.depthRuns([T(1), L(1)]);
  assert.deepStrictEqual(runs.map(r => [r.from, r.to, r.isTotal]), [[0, 0, true], [1, 1, false]]);
});

test('runs cover every row exactly once, in order', () => {
  const rows = [T(0), T(1), L(2), L(2), T(1), L(2), L(3), L(2)];
  const runs = lib.depthRuns(rows);
  let expected = 0;
  runs.forEach(r => {
    assert.strictEqual(r.from, expected, 'runs must be contiguous');
    assert.ok(r.to >= r.from);
    expected = r.to + 1;
  });
  assert.strictEqual(expected, rows.length, 'runs must cover the whole cube');
});

test('an empty cube produces no runs', () => {
  assert.deepStrictEqual(lib.depthRuns([]), []);
  assert.deepStrictEqual(lib.depthRuns(undefined), []);
});

/* ── dimf: the dimension-filter parameter ───────────────────────────────
   It is part of the comment anchor, so a change in what it emits silently
   re-points every saved comment. */

test('no filters means no dimf at all, not an empty one', () => {
  assert.deepStrictEqual(lib.dimfParam({}), {});
  assert.deepStrictEqual(lib.dimfParam({ filters: {} }), {});
  assert.deepStrictEqual(lib.dimfParam({ filters: { fin_year: [] } }), {},
    'a field on Filters but not narrowed passes everything through');
  assert.deepStrictEqual(lib.dimfParam(undefined), {});
});

test('dimf keeps the order the user arranged, unsorted', () => {
  const out = lib.dimfParam({ filters: { fin_year: ['FY2026', 'FY2024', 'FY2025'] } });
  assert.strictEqual(out.dimf, '{"fin_year":["FY2026","FY2024","FY2025"]}');
});

test('dimf does not alias the caller\'s arrays', () => {
  const filters = { fin_year: ['FY2026'] };
  lib.dimfParam({ filters: filters });
  assert.deepStrictEqual(filters.fin_year, ['FY2026']);
});

/* ── the anchor's dimf ─────────────────────────────────────────────────
   A subset naming EVERY member narrows nothing, so the anchor writes it the
   way it writes "no filter": omitted. This is what stopped a comment made in
   the add-in from carrying twelve years and a hundred and forty-four months
   into the console. It collapses only what it can PROVE is all-members --
   a wrong collapse re-points a comment at a figure it never described. */

test('an all-members subset is omitted from the anchor', () => {
  const spec = { filters: { year: ['2025', '2026'] } };
  const totals = { year: { members: ['2025', '2026'], truncated: false } };
  assert.deepStrictEqual(lib.anchorDimfParam(spec, totals), {},
    'every member selected is the same cut as no filter');
});

test('a genuinely narrowed subset survives in the anchor', () => {
  const spec = { filters: { year: ['2026'] } };
  const totals = { year: { members: ['2025', '2026'], truncated: false } };
  assert.strictEqual(lib.anchorDimfParam(spec, totals).dimf, '{"year":["2026"]}');
});

test('the anchor collapses each dimension independently', () => {
  const spec = { filters: { year: ['2025', '2026'], entity: ['Klikk'] } };
  const totals = {
    year: { members: ['2025', '2026'], truncated: false },
    entity: { members: ['Klikk', 'Tremly'], truncated: false },
  };
  assert.strictEqual(lib.anchorDimfParam(spec, totals).dimf, '{"entity":["Klikk"]}');
});

test('no member list, an empty one, or a capped one never collapses', () => {
  const spec = { filters: { year: ['2025', '2026'] } };
  const full = '{"year":["2025","2026"]}';
  assert.strictEqual(lib.anchorDimfParam(spec, {}).dimf, full,
    'a members fetch that failed must not read as "all"');
  assert.strictEqual(lib.anchorDimfParam(spec, undefined).dimf, full);
  assert.strictEqual(
    lib.anchorDimfParam(spec, { year: { members: [], truncated: false } }).dimf, full,
    'an empty member list proves nothing');
  assert.strictEqual(
    lib.anchorDimfParam(spec, { year: { members: ['2025', '2026'], truncated: true } }).dimf,
    full, 'a capped list cannot prove the subset covers every member');
});

test('the anchor tests set coverage, not list length', () => {
  // A subset can hold a value no longer in the member list because the journal
  // filters moved under it -- equal lengths, different sets.
  const spec = { filters: { year: ['2026', '2099'] } };
  const totals = { year: { members: ['2025', '2026'], truncated: false } };
  assert.strictEqual(lib.anchorDimfParam(spec, totals).dimf, '{"year":["2026","2099"]}',
    '2025 is unselected, so this narrows and must be kept');
});

test('an empty subset stays omitted, and the anchor keeps subset order', () => {
  assert.deepStrictEqual(lib.anchorDimfParam({ filters: { year: [] } }, {}), {});
  assert.deepStrictEqual(lib.anchorDimfParam({}, {}), {});
  assert.deepStrictEqual(lib.anchorDimfParam(undefined, undefined), {});
  const out = lib.anchorDimfParam(
    { filters: { fin_year: ['FY2026', 'FY2024'] } },
    { fin_year: { members: ['FY2024', 'FY2025', 'FY2026'], truncated: false } });
  assert.strictEqual(out.dimf, '{"fin_year":["FY2026","FY2024"]}');
});

test('the anchor does not alias the caller\'s arrays', () => {
  const filters = { fin_year: ['FY2026'] };
  lib.anchorDimfParam({ filters: filters }, {});
  assert.deepStrictEqual(filters.fin_year, ['FY2026']);
});

test('the QUERY dimf never collapses, so a hand-made layout survives', () => {
  // dimfParam is the layout order. An all-members subset arranged by hand must
  // still reach the server in that arrangement -- this is why there are two
  // functions, and the reason they must not be merged.
  const spec = { filters: { year: ['2026', '2025'] } };
  const totals = { year: { members: ['2025', '2026'], truncated: false } };
  assert.strictEqual(lib.dimfParam(spec).dimf, '{"year":["2026","2025"]}');
  assert.deepStrictEqual(lib.anchorDimfParam(spec, totals), {});
});

/* ── rtotals / ctotals ─────────────────────────────────────────────────
   Sent only when they differ from the server's default, so an untouched cube
   makes the request it always made. */

test('untouched totals send nothing', () => {
  assert.deepStrictEqual(lib.totalsParams({ rows: ['a', 'b'], cols: ['c'] }), {});
});

test('row totals default on; switching one off names the survivors', () => {
  const out = lib.totalsParams({ rows: ['a', 'b', 'c'], cols: [], totals: { a: false } });
  assert.deepStrictEqual(out, { rtotals: 'b' });
});

test('switching every row parent off sends the explicit "none"', () => {
  const out = lib.totalsParams({ rows: ['a', 'b'], cols: [], totals: { a: false } });
  assert.deepStrictEqual(out, { rtotals: '__none__' },
    'an empty value would read as "unchanged"; the server needs "no subtotals"');
});

test('the innermost row field is never a subtotal candidate', () => {
  const out = lib.totalsParams({ rows: ['a', 'b'], cols: [], totals: { b: false } });
  assert.deepStrictEqual(out, {}, 'b is the leaf; turning it off changes nothing');
});

test('column totals default off; switching one on names it', () => {
  const out = lib.totalsParams({ rows: ['a'], cols: ['x', 'y'], totals: { x: true } });
  assert.deepStrictEqual(out, { ctotals: 'x' });
});

/* ── column header spans ───────────────────────────────────────────────── */

test('samePrefix compares every level up to and including the one asked for', () => {
  assert.ok(lib.samePrefix(['FY26', 'Q1'], ['FY26', 'Q2'], 0));
  assert.ok(!lib.samePrefix(['FY26', 'Q1'], ['FY26', 'Q2'], 1));
  assert.ok(lib.samePrefix(['FY26'], ['FY26', undefined], 1), 'a missing level reads as blank');
});

/* ── dates ─────────────────────────────────────────────────────────────── */

test('an ISO date becomes the Excel serial for that day', () => {
  assert.strictEqual(lib.toSerial('1900-01-01'), 2);
  assert.strictEqual(lib.toSerial('2026-09-03'), 46268);
});

test('anything that is not a date is passed through, not turned into a number', () => {
  assert.strictEqual(lib.toSerial(''), '');
  assert.strictEqual(lib.toSerial(null), '');
  assert.strictEqual(lib.toSerial('not a date'), 'not a date');
});

/* ── which sheet Build writes to ───────────────────────────────────────── */

test('a cube sheet of ours in front is the Build target', () => {
  const active = { id: 'sheet-7', binding: { kind: 'cube', spec: { rows: ['a'] } } };
  assert.strictEqual(lib.cubeTarget(active), 'sheet-7');
});

test('a detail sheet, a foreign sheet or no sheet means a new sheet', () => {
  assert.strictEqual(lib.cubeTarget({ id: 's1', binding: { kind: 'detail', spec: null } }), null);
  assert.strictEqual(lib.cubeTarget({ id: 's1', binding: null }), null);
  assert.strictEqual(lib.cubeTarget({ id: null, binding: { kind: 'cube', spec: {} } }), null);
  assert.strictEqual(lib.cubeTarget(null), null);
});

/* ── column widths across a rebuild ─────────────────────────────────────
   The behavioural side of this is test/widths.test.js, which drives the pane.
   These pin the mapping itself: which old column a new column inherits from,
   and what happens when there is no honest answer. */

test('a fresh sheet gets the defaults: outer row labels, the long one, values', () => {
  assert.deepStrictEqual(lib.cubeWidths({ nRowDims: 2, nCols: 3 }, null),
    [130, 330, 104, 104, 104, 104]);
  // One row dimension: that one column IS the long one.
  assert.deepStrictEqual(lib.cubeWidths({ nRowDims: 1, nCols: 1 }, null), [330, 104, 104]);
});

test('an unchanged layout keeps every width, defaults included', () => {
  const prev = { widths: [60, 240, 70, 150], nRowDims: 2 };
  assert.deepStrictEqual(lib.cubeWidths({ nRowDims: 2, nCols: 1 }, prev), [60, 240, 70, 150]);
});

test('the grand total is matched to the grand total, not to its index', () => {
  const prev = { widths: [60, 240, 70, 150], nRowDims: 2 };
  // A second period: the new column takes the default and 150 moves right.
  assert.deepStrictEqual(lib.cubeWidths({ nRowDims: 2, nCols: 2 }, prev),
    [60, 240, 70, 104, 150]);
});

test('a row-label column whose dimension went away takes the default', () => {
  const prev = { widths: [60, 240, 70, 150], nRowDims: 2 };
  assert.deepStrictEqual(lib.cubeWidths({ nRowDims: 1, nCols: 1 }, prev), [330, 70, 150]);
});

test('a width that could not be read falls back to the default for that column', () => {
  const prev = { widths: [null, null, 70, 150], nRowDims: 2 };
  assert.deepStrictEqual(lib.cubeWidths({ nRowDims: 2, nCols: 1 }, prev), [130, 330, 70, 150]);
});

test('a sheet too narrow to be a cube of ours is defaulted, not guessed at', () => {
  // Someone deleted columns by hand; there is no honest mapping left.
  assert.deepStrictEqual(lib.cubeWidths({ nRowDims: 2, nCols: 1 },
    { widths: [60, 240], nRowDims: 2 }), [130, 330, 104, 104]);
  assert.deepStrictEqual(lib.cubeWidths({ nRowDims: 2, nCols: 1 },
    { widths: [], nRowDims: 2 }), [130, 330, 104, 104]);
});

test('detail columns are fixed, so position is identity', () => {
  assert.deepStrictEqual(lib.detailWidths([80, 60, 90], null), [80, 60, 90]);
  assert.deepStrictEqual(lib.detailWidths([80, 60, 90], { widths: [30, null] }), [30, 60, 90]);
  // A sheet from an older, narrower COLUMNS list: the new columns default.
  assert.deepStrictEqual(lib.detailWidths([80, 60, 90], { widths: [30] }), [30, 60, 90]);
});

test('adjacent equal widths collapse into one range call', () => {
  assert.deepStrictEqual(lib.widthRuns([130, 330, 104, 104, 104]),
    [{ col: 0, span: 1, width: 130 },
     { col: 1, span: 1, width: 330 },
     { col: 2, span: 3, width: 104 }]);
  assert.deepStrictEqual(lib.widthRuns([]), []);
  assert.deepStrictEqual(lib.widthRuns([90]), [{ col: 0, span: 1, width: 90 }]);
});
