/* Author tests — the pane does not ask who you are, and does not claim to know.
 *
 * The failure this pins: the pane carried a free-text "Your name" box and put
 * whatever was in it on every comment, subset and saved view. A field the
 * client controls is a field that can disagree with the credential, and
 * app.cube_comments records exactly what that costs — `ewffew` x12 (a keyboard
 * mash that became a durable author), `test`, `test2`, MC's own notes split
 * across author_key 'MC' and '', and 55 rows authored by nobody at all.
 *
 * The server now stamps the author from the token that posted. So the checks
 * here are about what the CLIENT does: it must not send an author, and it must
 * not grow the box back. Both are asserted against the shipped files rather
 * than a copy, so a re-introduction fails here before it reaches the register.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { loadPane } = require('./fake-host.js');
const { ROUTES } = require('./fixtures.js');

const ADDIN_DIR = process.env.KLIKK_ADDIN_DIR
  ? path.resolve(process.env.KLIKK_ADDIN_DIR)
  : path.resolve(__dirname, '..');

const IDENTITY = '/journals/pivot/comments/identity/';

/* The identity endpoint answers as the server would for MC's token: the
   operator behind the shared `excel-addin` credential, stamped, not declared.

   Listed FIRST because makeFetch takes the first route whose fragment appears
   in the URL, and '/journals/pivot/' (the cube) is a prefix of this path. */
const ROUTES_WITH_IDENTITY = Object.assign(
  { [IDENTITY]: { author: 'mc@tremly.com', author_key: 'mc@tremly.com', verified: true, stamped: true } },
  ROUTES
);

async function connectedPane(routes) {
  const pane = await loadPane({ routes: routes || ROUTES_WITH_IDENTITY });
  await pane.boot();
  pane.$('token').value = 'test-token';
  pane.click('btnConnect');
  await pane.settle(20);
  assert.match(pane.$('connLabel').textContent, /Connected/,
    'the fixture did not satisfy connect(): ' + pane.$('settingsMsg').textContent);
  return pane;
}

function posted(pane, fragment) {
  return pane.rec.requests
    .filter(r => r.method === 'POST' && r.url.indexOf(fragment) >= 0)
    .map(r => JSON.parse(r.body || '{}'));
}

test('there is no name box, in the HTML or in the JS', () => {
  const html = fs.readFileSync(path.join(ADDIN_DIR, 'taskpane.html'), 'utf8');
  const js = fs.readFileSync(path.join(ADDIN_DIR, 'app.js'), 'utf8');
  assert.ok(!/id="commentAuthor"/.test(html),
    'taskpane.html grew the "Your name" box back — the server stamps the author now');
  assert.ok(!/commentAuthor/.test(js),
    'app.js still reads a typed author; the server ignores it, so the two would disagree');
});

test('nothing the pane POSTs carries an author', async () => {
  // Every write the pane makes, on one connected session: a saved subset, a
  // saved view. Each used to carry the typed name.
  const pane = await connectedPane();
  try {
    pane.click('btnCube');
    await pane.settle(30);

    pane.$('viewName').value = 'Test view';
    pane.click('btnViewSave');
    await pane.settle(30);

    const views = posted(pane, '/journals/pivot/views/');
    assert.strictEqual(views.length, 1, 'the saved-view POST did not happen');
    assert.ok(!('author' in views[0]),
      'the pane still sends an author on a saved view: ' + JSON.stringify(views[0]));

    // And no POST anywhere in the session smuggles one in.
    const all = pane.rec.requests.filter(r => r.method === 'POST' && r.body);
    for (const r of all) {
      assert.ok(!('author' in JSON.parse(r.body)),
        'a POST to ' + r.url + ' still carries an author');
    }
  } finally { pane.close(); }
});

test('the pane reports whose name it will sign with, from the server', async () => {
  const pane = await connectedPane();
  try {
    assert.match(pane.$('commentIdentity').textContent, /mc@tremly\.com/,
      'the pane never showed the stamped author');
    assert.match(pane.$('commentIdentity').textContent, /not typed/,
      'a stamped identity must say so — that is the whole change');
  } finally { pane.close(); }
});

test('an older backend without the endpoint costs a caption, not the connection', async () => {
  // ROUTES has no identity route, so it answers 404 exactly as a backend
  // deployed before this change does.
  const pane = await connectedPane(ROUTES);
  try {
    assert.match(pane.$('connLabel').textContent, /Connected/);
    assert.strictEqual(pane.$('errorMsg').textContent, '',
      'a missing identity endpoint must not surface as an error');
    assert.match(pane.$('commentIdentity').textContent, /signed with the account/i,
      'the neutral caption must survive a 404');
  } finally { pane.close(); }
});
