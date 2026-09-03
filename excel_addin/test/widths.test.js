/* Column-width tests — a rebuild must not undo MC's sizing.
 *
 * The 2026-09-03 in-place rebuild peels groups, unfreezes panes and clears the
 * range, then re-applies the default widths from the layout. MC sizes those
 * columns by hand — the outer ones narrow, the last one wide, so the row-label
 * hierarchy reads — and every Build threw that away. His words: "when I
 * rebuild the cube it resets my column spacing".
 *
 * The test drives it the way he hit it: build, drag the column edges, rebuild,
 * look at the widths. Dragging is a write straight into the fake sheet's width
 * map, because that is what the mouse does — no add-in code involved.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { loadPane } = require('./fake-host.js');
const { ROUTES, CUBE } = require('./fixtures.js');

/* The defaults, as app.js/lib.js apply them: 130 for each outer row-label
   column, 330 for the innermost (the long account name), 104 per value column
   and for the grand total. */
const DEF = { rowDim: 130, rowLeaf: 330, value: 104 };

/* A cube of the fixture's shape with `n` column members — one more month is
   one more column, which is how the layout actually changes between builds. */
function cubeWithCols(n) {
  const cols = [];
  for (let i = 0; i < n; i++) cols.push('FY' + (2026 + i));
  return Object.assign({}, CUBE, {
    cols: cols,
    col_paths: cols.map(c => [c]),
    rows: CUBE.rows.map(r => Object.assign({}, r, { cells: cols.map(() => 100) })),
    col_totals: cols.map(() => 100)
  });
}

/* One row dimension instead of two: the row-label columns stop meaning what
   they meant, so their widths must NOT carry across. */
function cubeWithOneRowDim() {
  return Object.assign({}, cubeWithCols(1), {
    row_dims: [{ key: 'account', label: 'Account' }],
    rows: [{ keys: ['Sales'], cells: [100], depth: 0, is_total: false }]
  });
}

async function paneOn(cubeRef) {
  const routes = Object.assign({}, ROUTES, { '/journals/pivot/': () => cubeRef.cube });
  const pane = await loadPane({ routes: routes });
  await pane.boot();
  pane.$('token').value = 'test-token';
  pane.click('btnConnect');
  await pane.settle(20);
  assert.match(pane.$('connLabel').textContent, /Connected/,
    'the fixture did not satisfy connect(): ' + pane.$('settingsMsg').textContent);
  return pane;
}

async function build(pane) {
  pane.click('btnCube');
  await pane.settle(30);
  assert.strictEqual(pane.$('errorMsg').textContent, '', 'Build reported an error');
}

function sheet(pane) {
  const s = pane.rec.workbook.sheets.find(x => x.name === 'Cube');
  assert.ok(s, 'no Cube sheet was built');
  return s;
}

function widths(pane, n) {
  const s = sheet(pane);
  const out = [];
  for (let i = 0; i < n; i++) out.push(s.colWidths[i]);
  return out;
}

// What MC does with the mouse.
function drag(pane, byCol) {
  const s = sheet(pane);
  Object.keys(byCol).forEach(k => { s.colWidths[k] = byCol[k]; });
}

test('a first build sizes the columns for the layout', async () => {
  const ref = { cube: cubeWithCols(1) };
  const pane = await paneOn(ref);
  try {
    await build(pane);
    // two row dims, one column, one grand total
    assert.deepStrictEqual(widths(pane, 4),
      [DEF.rowDim, DEF.rowLeaf, DEF.value, DEF.value]);
  } finally { pane.close(); }
});

test('a rebuild keeps every column MC sized by hand', async () => {
  const ref = { cube: cubeWithCols(1) };
  const pane = await paneOn(ref);
  try {
    await build(pane);
    const mine = { 0: 60, 1: 240, 2: 70, 3: 150 };
    drag(pane, mine);

    await build(pane);
    assert.deepStrictEqual(widths(pane, 4), [60, 240, 70, 150],
      'the rebuild re-applied the defaults over MC\'s sizing');
  } finally { pane.close(); }
});

test('five rebuilds later the sizing is still his', async () => {
  // "Rebuild on every change" fires a build per drag of the wells.
  const ref = { cube: cubeWithCols(1) };
  const pane = await paneOn(ref);
  try {
    await build(pane);
    drag(pane, { 0: 60, 1: 240, 2: 70, 3: 150 });
    for (let i = 0; i < 5; i++) await build(pane);
    assert.deepStrictEqual(widths(pane, 4), [60, 240, 70, 150]);
  } finally { pane.close(); }
});

test('a new month gets the default; the columns either side keep their width', async () => {
  const ref = { cube: cubeWithCols(1) };
  const pane = await paneOn(ref);
  try {
    await build(pane);
    drag(pane, { 0: 60, 1: 240, 2: 70, 3: 150 });

    // Same query, one more period: the grand total moves right by one.
    ref.cube = cubeWithCols(2);
    await build(pane);
    assert.deepStrictEqual(widths(pane, 5), [60, 240, 70, DEF.value, 150],
      'a saved width was stretched onto the column that took its place');
  } finally { pane.close(); }
});

test('a column that disappears does not shift its width onto its neighbour', async () => {
  const ref = { cube: cubeWithCols(3) };
  const pane = await paneOn(ref);
  try {
    await build(pane);
    drag(pane, { 0: 60, 1: 240, 2: 70, 3: 80, 4: 90, 5: 150 });

    ref.cube = cubeWithCols(2);
    await build(pane);
    assert.deepStrictEqual(widths(pane, 5), [60, 240, 70, 80, 150],
      'the k-th value column must keep the k-th value column\'s width');
  } finally { pane.close(); }
});

test('dropping a row dimension re-defaults the row-label columns, not the values', async () => {
  /* Old column 0 held a class code at 60; new column 0 holds the account name.
     Carrying 60 across would clip it — that is the "unrelated column" case. */
  const ref = { cube: cubeWithCols(1) };
  const pane = await paneOn(ref);
  try {
    await build(pane);
    drag(pane, { 0: 60, 1: 240, 2: 70, 3: 150 });

    ref.cube = cubeWithOneRowDim();
    await build(pane);
    assert.deepStrictEqual(widths(pane, 3), [DEF.rowLeaf, 70, 150],
      'the row-label column kept a width that belonged to a different dimension');
  } finally { pane.close(); }
});

test('New sheet starts from the defaults, not from the sized sheet', async () => {
  const ref = { cube: cubeWithCols(1) };
  const pane = await paneOn(ref);
  try {
    await build(pane);
    drag(pane, { 0: 60, 1: 240, 2: 70, 3: 150 });
    pane.click('btnCubeNew');
    await pane.settle(30);
    const fresh = pane.rec.workbook.sheets.find(x => x.name === 'Cube 2');
    assert.ok(fresh, 'New sheet did not add a sheet');
    assert.deepStrictEqual([0, 1, 2, 3].map(i => fresh.colWidths[i]),
      [DEF.rowDim, DEF.rowLeaf, DEF.value, DEF.value]);
    // …and it left the sized sheet alone.
    assert.deepStrictEqual(widths(pane, 4), [60, 240, 70, 150]);
  } finally { pane.close(); }
});

test('widths are written in runs, not one call per column', async () => {
  /* Per-column Office calls are what make a wide cube appear to hang, and a
     cube is routinely fifty columns. Adjacent equal widths collapse into one
     call, so a fresh 14-column cube costs the same three calls the three
     literals used to. */
  const ref = { cube: cubeWithCols(12) };
  const pane = await paneOn(ref);
  try {
    await build(pane);
    const cube = sheet(pane).id;
    const sets = pane.rec.widthWrites.filter(w => w.sheet === cube);
    assert.deepStrictEqual(sets.map(w => [w.col, w.cols, w.width]),
      [[0, 1, DEF.rowDim], [1, 1, DEF.rowLeaf], [2, 12 + 1, DEF.value]],
      'the defaults must still cost three calls');

    // A hand-sized sheet costs at most one call per distinct adjacent width.
    drag(pane, { 0: 60, 1: 240 });
    pane.rec.widthWrites.length = 0;
    await build(pane);
    assert.ok(pane.rec.widthWrites.length <= 4,
      'a rebuild set widths ' + pane.rec.widthWrites.length + ' times; that is per-column');
  } finally { pane.close(); }
});
