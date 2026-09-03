/* Boot tests — the pane loaded, and every listener attached.
 *
 * The 2026-09-03 failure this pins: wireEvents() registered 40+ listeners with
 * bare el.X.addEventListener. One missing id threw, and every listener AFTER
 * it was never attached — while the pane still rendered, chrome and all. Half
 * the buttons were dead and nothing on screen said why. The on() helper fixed
 * it; the test that keeps it fixed has to prove the LATER listeners survive a
 * missing EARLIER control, which is why it asserts on listener registration
 * and not on "did the pane render".
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { loadPane } = require('./fake-host.js');
const { ROUTES } = require('./fixtures.js');

// Wired at the very end of wireEvents(): if these attached, nothing before
// them threw and took the rest of the chain down.
const LAST_WIRED = ['btnRefresh', 'btnRestore', 'btnCancel', 'journalType'];

function wiredIds(pane) {
  return new Set(pane.rec.listeners.map(l => l.id));
}

test('the pane boots clean on a matching page', async () => {
  const pane = await loadPane({ routes: ROUTES });
  try {
    await pane.boot();
    const d = pane.diag();
    assert.strictEqual(d.bootDone, true, 'boot did not finish; last step: ' + d.step);
    assert.deepStrictEqual(d.missingControls, [], 'taskpane.html is missing controls app.js wires');
    assert.deepStrictEqual(d.errors, [], 'script errors during boot');
    assert.strictEqual(pane.$('boot').hidden, true, 'the "Loading…" panel must be gone');
    assert.strictEqual(pane.$('app').hidden, false, 'the pane body must be shown');
  } finally { pane.close(); }
});

test('every control app.js wires exists in taskpane.html', async () => {
  // missingControls is the pane's own answer to "did the HTML and the JS come
  // from the same release". It is only meaningful once the cube panel has
  // been wired too, which happens on connect.
  const pane = await loadPane({ routes: ROUTES });
  try {
    await pane.boot();
    pane.$('token').value = 'test-token';
    pane.click('btnConnect');
    await pane.settle(20);
    assert.deepStrictEqual(pane.diag().missingControls, []);
  } finally { pane.close(); }
});

test('a missing control costs that one button, not the ones after it', async () => {
  // btnLoad is wired near the START of wireEvents(). Before the on() helper,
  // deleting it killed every listener registered after it.
  const pane = await loadPane({ routes: ROUTES, removeControls: ['btnLoad'] });
  try {
    await pane.boot();
    const d = pane.diag();
    assert.strictEqual(d.bootDone, true, 'boot must survive a missing control');
    assert.deepStrictEqual(d.missingControls, ['btnLoad']);

    const ids = wiredIds(pane);
    for (const id of LAST_WIRED) {
      assert.ok(ids.has(id), id + ' never got a listener — the wiring chain broke at btnLoad');
    }

    // And the pane must SAY so, rather than looking fine with dead buttons.
    assert.strictEqual(pane.$('errorMsg').hidden, false);
    assert.match(pane.$('errorMsg').textContent, /btnLoad/);
  } finally { pane.close(); }
});

test('several missing controls are all reported, and the rest still wire', async () => {
  const gone = ['btnCount', 'btnDrill', 'btnBulkFlag'];
  const pane = await loadPane({ routes: ROUTES, removeControls: gone });
  try {
    await pane.boot();
    assert.strictEqual(pane.diag().bootDone, true);
    assert.deepStrictEqual(pane.diag().missingControls.sort(), gone.slice().sort());
    const ids = wiredIds(pane);
    for (const id of LAST_WIRED) assert.ok(ids.has(id), id + ' never got a listener');
  } finally { pane.close(); }
});

/* FOUND BY THIS HARNESS, 2026-09-03, NOT YET FIXED.
 *
 * on() hardened the LISTENERS against a missing control, and setButtons()
 * goes through the null-safe setDisabled(). paintRefreshPanel() does not: it
 * sets .disabled directly on six controls (app.js:919-924, 938-943) with no
 * null check. Delete btnPivot from the page and inspectActiveSheet() rejects on
 * the boot path — the pane comes up, but the Refresh panel is never painted
 * and nothing says why. That is the same failure of 2026-09-03, one function
 * along.
 *
 * Left as a todo rather than fixed here, because this commit is the harness
 * and must not change add-in behaviour. Deleting `todo: true` is the check
 * that the fix worked. */
test('a missing control does not break the panels either', { todo: 'paintRefreshPanel still dereferences optional controls' }, async () => {
  const pane = await loadPane({ routes: ROUTES, removeControls: ['btnPivot'] });
  try {
    await pane.boot();
    assert.deepStrictEqual(pane.rec.pageErrors, [], 'a missing control must not reject on the boot path');
    assert.match(pane.$('sheetInfo').textContent, /\S/, 'the Refresh panel was never painted');
  } finally { pane.close(); }
});

test('app.js refuses to run without lib.js, loudly', async () => {
  // HTML and JS are cached independently, so a new app.js can meet an old
  // page with no <script src="lib.js">. That must be a sentence on the boot
  // panel, not "LIB is undefined" in a console nobody opens.
  const pane = await loadPane({ routes: ROUTES, skipLib: true });
  try {
    assert.match(pane.$('boot').textContent, /lib\.js/);
    assert.strictEqual(pane.window.__klikkAppReportedError, true);
  } finally { pane.close(); }
});

/* ── dead handlers, at runtime ──────────────────────────────────────────
   The lint gate (npm run lint, no-undef) catches a handler that calls a
   deleted function by reading the source. This catches the same class by
   clicking: run() swallows the ReferenceError into the error line, so a dead
   button looks like an ordinary failure rather than a crash. Both gates are
   cheap; the two-week-old reloadThisSheet needed only one of them. */

const EVERY_BUTTON = [
  'btnSettings', 'btnConnect', 'btnLoad', 'btnCount', 'btnCube', 'btnCubeNew',
  'btnPivot', 'btnReload', 'btnSyncComments', 'btnResetComments', 'btnFullPivot',
  'btnPushComments', 'btnSaveComment', 'btnDeleteComment', 'btnDrill', 'btnBulkFlag',
  'btnViewSave', 'btnViewLoad', 'btnViewRebuild', 'btnViewDelete', 'btnRefresh',
  'btnRestore', 'btnCancel', 'btnSubsetLoad', 'btnSubsetSave', 'btnSubsetDelete',
  'btnPickAdd', 'btnPickAddAll', 'btnPickRemove', 'btnPickRemoveAll',
  'btnPickUp', 'btnPickDown', 'btnPickSortAz', 'btnForget'
];

test('clicking every button raises no ReferenceError', async () => {
  const pane = await loadPane({ routes: ROUTES });
  try {
    await pane.boot();
    pane.$('token').value = 'test-token';
    pane.click('btnConnect');
    await pane.settle(20);

    const dead = [];
    for (const id of EVERY_BUTTON) {
      assert.ok(pane.$(id), 'taskpane.html has no ' + id);
      pane.$('errorMsg').textContent = '';
      pane.click(id);
      await pane.settle(4);
      // run() catches, so a dead handler surfaces here rather than throwing.
      if (/is not defined|is not a function/.test(pane.$('errorMsg').textContent)) {
        dead.push(id + ': ' + pane.$('errorMsg').textContent);
      }
    }
    assert.deepStrictEqual(dead, [], 'buttons calling something that no longer exists');

    const refErrors = pane.rec.pageErrors.filter(e => /is not defined|is not a function/.test(e));
    assert.deepStrictEqual(refErrors, [], 'uncaught reference errors from a click');
  } finally { pane.close(); }
});
