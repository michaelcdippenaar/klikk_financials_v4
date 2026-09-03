/* Build tests — one sheet, not five.
 *
 * The 2026-09-03 failure this pins: buildCube() added a worksheet on every
 * call, so "Rebuild on every change" produced Cube, Cube 2 … Cube 5 in eleven
 * seconds of dragging (three bindings at 08:03:33, :42 and :44 in one
 * workbook) and a layout could never be edited, only re-created. cubeTargetId()
 * fixed it by rewriting the bound sheet in place.
 *
 * The test drives the pane the way MC found the bug: click Build, click Build
 * again, count the sheets.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { loadPane } = require('./fake-host.js');
const { ROUTES } = require('./fixtures.js');

async function connectedPane() {
  const pane = await loadPane({ routes: ROUTES });
  await pane.boot();
  pane.$('token').value = 'test-token';
  pane.click('btnConnect');
  await pane.settle(20);
  assert.match(pane.$('connLabel').textContent, /Connected/,
    'the fixture did not satisfy connect(): ' + pane.$('settingsMsg').textContent);
  return pane;
}

async function build(pane, id) {
  pane.click(id || 'btnCube');
  await pane.settle(30);
  assert.strictEqual(pane.$('errorMsg').textContent, '', 'Build reported an error');
  return pane.$('cubeMsg').textContent;
}

test('a second Build rewrites the cube sheet instead of adding one', async () => {
  const pane = await connectedPane();
  try {
    const first = await build(pane);
    assert.deepStrictEqual(pane.rec.sheetsAdded, ['Cube'], 'the first Build makes the sheet');
    assert.match(first, /written to Cube/);

    const second = await build(pane);
    assert.deepStrictEqual(pane.rec.sheetsAdded, ['Cube'],
      'the second Build added another sheet — this is Cube 2');
    assert.match(second, /rebuilt in place on Cube/);
  } finally { pane.close(); }
});

test('five rapid Builds leave one sheet, not five', async () => {
  // Eleven seconds of dragging the wells is what produced Cube 2 … Cube 5.
  const pane = await connectedPane();
  try {
    for (let i = 0; i < 5; i++) await build(pane);
    assert.deepStrictEqual(pane.rec.sheetsAdded, ['Cube']);
    const names = pane.rec.workbook.sheets.map(s => s.name);
    assert.deepStrictEqual(names, ['Sheet1', 'Cube'],
      'the workbook grew a sheet per Build: ' + names.join(', '));
  } finally { pane.close(); }
});

test('the Build button says which of the two it will do', async () => {
  const pane = await connectedPane();
  try {
    assert.strictEqual(pane.$('btnCube').textContent, 'Build cube view');
    assert.strictEqual(pane.$('btnCubeNew').hidden, true, 'nothing to leave alone yet');
    await build(pane);
    assert.strictEqual(pane.$('btnCube').textContent, 'Rebuild Cube');
    assert.strictEqual(pane.$('btnCubeNew').hidden, false, '"New sheet" must be offered');
  } finally { pane.close(); }
});

test('New sheet still makes a new sheet', async () => {
  // The escape hatch from in-place rebuilding, shipped alongside it.
  const pane = await connectedPane();
  try {
    await build(pane);
    await build(pane, 'btnCubeNew');
    assert.deepStrictEqual(pane.rec.sheetsAdded, ['Cube', 'Cube 2'],
      'btnCubeNew must always add, never rewrite');
  } finally { pane.close(); }
});

test('the built sheet is bound as a cube, which is what makes it the target', async () => {
  const pane = await connectedPane();
  try {
    await build(pane);
    const store = pane.rec.office._settings;
    const keys = Object.keys(store).filter(k => k.indexOf('sheet-2') >= 0);
    assert.strictEqual(keys.length, 1, 'the cube sheet was not bound in the workbook');
    const binding = JSON.parse(store[keys[0]]);
    assert.strictEqual(binding.kind, 'cube');
    assert.ok(binding.spec && binding.spec.rows.length, 'the layout must be saved with the sheet');
  } finally { pane.close(); }
});
